"""Forward-only SQL migrations (ADR 0003, NFR-15).

Files in `migrations/` run in name order, each in its own transaction, under an advisory
lock, so any number of servers and workers can start at once. A file that changed after
it was applied stops everything: applied migrations are history, not drafts.
"""

import asyncio
import hashlib
import logging
from importlib.resources import files
from typing import Any

import asyncpg

from trimset_server.partitions import ensure_partitions

log = logging.getLogger("trimset.migrate")
_LOCK = 7_246_521_001  # pg advisory lock for migrations


class MigrationError(Exception):
    pass


def available() -> list[tuple[str, bytes]]:
    """(version, sql) for every migration shipped in this package, in order."""
    folder = files("trimset_server").joinpath("migrations")
    found = [(f.name.removesuffix(".sql"), f.read_bytes()) for f in folder.iterdir()]
    return sorted((v, sql) for v, sql in found if v[:4].isdigit())


async def migrate(conn: Any) -> list[str]:
    """Apply what's missing, then make sure the partitions exist. Returns new versions."""
    await conn.execute("SELECT pg_advisory_lock($1)", _LOCK)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY,"
            " checksum text NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
        applied = {r["version"]: r["checksum"] for r in rows}
        shipped = available()
        newer = set(applied) - {v for v, _ in shipped}
        if newer:
            log.warning("database has migrations this release doesn't know: %s", sorted(newer))
        done = []
        for version, sql in shipped:
            checksum = hashlib.sha256(sql).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise MigrationError(f"migration {version} changed after it was applied")
                continue
            async with conn.transaction():
                await conn.execute(sql.decode("utf-8"))
                await conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES ($1, $2)",
                    version,
                    checksum,
                )
            log.info("applied migration %s", version)
            done.append(version)
        created = await ensure_partitions(conn)
        if created:
            log.info("created %d partitions", len(created))
        return done
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK)


async def run_migrations(dsn: str, attempts: int = 10, delay: float = 3.0) -> list[str]:
    """Connect (Postgres may still be starting), migrate, disconnect."""
    for attempt in range(1, attempts + 1):
        try:
            conn = await asyncpg.connect(dsn, timeout=5)
            break
        except (OSError, TimeoutError, asyncpg.CannotConnectNowError) as e:
            if attempt == attempts:
                raise
            log.warning("database not reachable yet (%s), retrying", type(e).__name__)
            await asyncio.sleep(delay)
    try:
        return await migrate(conn)
    finally:
        await conn.close()

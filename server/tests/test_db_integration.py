"""Against a real Postgres. CI sets TRIMSET_TEST_DATABASE_URL; without it these skip."""

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from trimset_server.config import Settings
from trimset_server.migrate import MigrationError, available, run_migrations
from trimset_server.partitions import ensure_partitions, partition_months
from trimset_server.worker import run_worker

ADMIN_URL = os.environ.get("TRIMSET_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.anyio,
    pytest.mark.integration,
    pytest.mark.skipif(not ADMIN_URL, reason="needs TRIMSET_TEST_DATABASE_URL (CI has Postgres)"),
]


@pytest.fixture
async def dsn() -> AsyncIterator[str]:
    """A fresh, empty database for each test."""
    name = "trimset_test_" + uuid.uuid4().hex[:12]
    admin = await asyncpg.connect(ADMIN_URL)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        yield urlunsplit(urlsplit(ADMIN_URL)._replace(path="/" + name))
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _partitions(conn: Any) -> set[str]:
    rows = await conn.fetch(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = 'decisions'"
    )
    return {r["relname"] for r in rows}


@pytest.mark.req("NFR-15")
async def test_migrations_apply_once_and_create_partitions(dsn: str) -> None:
    assert await run_migrations(dsn) == ["0001_capture"]
    assert await run_migrations(dsn) == []

    conn = await asyncpg.connect(dsn)
    try:
        checksum = await conn.fetchval(
            "SELECT checksum FROM schema_migrations WHERE version = '0001_capture'"
        )
        assert checksum == hashlib.sha256(dict(available())["0001_capture"]).hexdigest()
        expected = {f"decisions_p{m:%Y_%m}" for m in partition_months(datetime.now(UTC), 400)}
        assert expected <= await _partitions(conn)
    finally:
        await conn.close()


@pytest.mark.req("NFR-15")
async def test_concurrent_starts_apply_each_migration_once(dsn: str) -> None:
    results = await asyncio.gather(*(run_migrations(dsn) for _ in range(3)))
    assert sorted(v for applied in results for v in applied) == ["0001_capture"]


@pytest.mark.req("NFR-15")
async def test_a_migration_edited_after_it_ran_is_refused(dsn: str) -> None:
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("UPDATE schema_migrations SET checksum = 'edited'")
    finally:
        await conn.close()
    with pytest.raises(MigrationError, match="0001_capture changed"):
        await run_migrations(dsn)


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "project_id": uuid.uuid4(),
        "ts": datetime.now(UTC),
        "decision_id": uuid.uuid4(),
        "question_id": "q_" + "0" * 32,
        "type": "noul",
        "format": "systemone",
        "model_requested": "jev-latest",
        "model_served": "jev-1.14.0",
        "probs": [0.83],
        "refusal": False,
        "role": "primary",
        "shadow_of": None,
    }
    return row | overrides


async def _insert(conn: Any, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    params = ", ".join(f"${i}" for i in range(1, len(row) + 1))
    await conn.execute(f"INSERT INTO decisions ({cols}) VALUES ({params})", *row.values())  # noqa: S608


@pytest.mark.req("FR-01")
@pytest.mark.req("FR-02")
async def test_decisions_keep_good_rows_and_refuse_bad_ones(dsn: str) -> None:
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        await _insert(conn, _row())
        await _insert(conn, _row(refusal=True, probs=None))
        await _insert(conn, _row(role="shadow", shadow_of=uuid.uuid4()))
        await _insert(conn, _row(ts=datetime.now(UTC) - timedelta(days=380)))  # late, in retention

        bad = [
            _row(refusal=True),  # a refusal with probabilities
            _row(probs=None),  # no probabilities and no refusal
            _row(probs=[1.5]),  # outside [0, 1]
            _row(role="shadow"),  # a shadow with no primary
            _row(type="poll"),
            _row(ts=datetime.now(UTC) + timedelta(days=400)),  # no partition that far ahead
        ]
        for row in bad:
            with pytest.raises(asyncpg.PostgresError):
                await _insert(conn, row)
        assert await conn.fetchval("SELECT count(*) FROM decisions") == 4
    finally:
        await conn.close()


@pytest.mark.req("FR-56")
async def test_partitions_follow_the_clock_and_the_longest_retention(dsn: str) -> None:
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        later = await ensure_partitions(conn, datetime.now(UTC) + timedelta(days=200))
        assert later, "months further ahead need new partitions"
        assert await ensure_partitions(conn, datetime.now(UTC) + timedelta(days=200)) == []

        await conn.execute(
            "INSERT INTO projects (project_id, name, settings) VALUES ($1, 'long', $2)",
            uuid.uuid4(),
            '{"retention_days": 800}',
        )
        older = await ensure_partitions(conn)
        oldest = partition_months(datetime.now(UTC), 800)[0]
        assert f"decisions_p{oldest:%Y_%m}" in older
    finally:
        await conn.close()


async def test_the_worker_runs_until_told_to_stop(dsn: str) -> None:
    await run_migrations(dsn)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_worker(Settings(database_url=dsn, maintenance_interval_s=0.05), stop)
    )
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

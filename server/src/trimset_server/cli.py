"""`trimset-server`: the commands that run on the server itself (ADR 0002, FR-53)."""

import argparse
import asyncio
import json
import logging.config
import os
import signal
import sys
import uuid
from collections.abc import Sequence
from typing import Any

import asyncpg
import uvicorn

from trimset_server import audit, keys
from trimset_server.config import Settings
from trimset_server.logs import log_config
from trimset_server.migrate import MigrationError, run_migrations
from trimset_server.worker import run_worker


class CommandError(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trimset-server")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the API and the proxy")
    # All interfaces by default: the server runs in a container behind its port mapping.
    serve.add_argument("--host", default=os.environ.get("TRIMSET_HOST", "0.0.0.0"))  # noqa: S104
    serve.add_argument("--port", type=int, default=int(os.environ.get("TRIMSET_PORT", "8800")))
    serve.add_argument("--workers", type=int, default=int(os.environ.get("TRIMSET_WORKERS", "1")))
    commands.add_parser("migrate", help="apply database migrations and create partitions")
    commands.add_parser("worker", help="run background maintenance")

    project = commands.add_parser("create-project", help="create a project; prints its id")
    project.add_argument("name")
    key = commands.add_parser("create-key", help="create a key; prints it once")
    key.add_argument("project", help="the project's name")
    key.add_argument("--scope", choices=list(keys.PREFIXES), default="admin")
    key.add_argument("--label", default="created on the server")
    revoke = commands.add_parser("revoke-key", help="revoke any key by its id")
    revoke.add_argument("key_id", type=uuid.UUID)
    return parser


async def _admin_command(args: argparse.Namespace, dsn: str) -> dict[str, Any]:
    """The bootstrap commands: no API key needed, because they run on the server itself."""
    conn = await asyncpg.connect(dsn, timeout=5)
    try:
        if args.command == "create-project":
            try:
                return {"project_id": str(await keys.create_project(conn, args.name))}
            except asyncpg.UniqueViolationError:
                raise CommandError(f"a project named {args.name!r} already exists") from None
        if args.command == "create-key":
            project_id = await keys.project_id_by_name(conn, args.project)
            if project_id is None:
                raise CommandError(f"no project named {args.project!r}")
            async with conn.transaction():
                row, secret = await keys.create_key(conn, project_id, args.scope, args.label)
                await audit.key_created(conn, project_id, None, row)
            return {"key_id": str(row["key_id"]), "scope": args.scope, "key": secret}
        async with conn.transaction():
            found = await keys.revoke_key(conn, args.key_id, None)
            await audit.key_revoked(conn, args.key_id, None, found)
        if found is None:
            raise CommandError(f"no key with id {args.key_id}")
        return {"revoked": str(args.key_id)}
    except asyncpg.UndefinedTableError:
        raise CommandError("the database isn't migrated: run `trimset-server migrate`") from None
    finally:
        await conn.close()


async def _worker(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run_worker(settings, stop)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except ValueError as e:
        print(f"trimset-server: {e}", file=sys.stderr)
        return 2
    long_running = args.command in ("serve", "worker")
    stream = "ext://sys.stdout" if long_running else "ext://sys.stderr"
    logging.config.dictConfig(log_config(settings.log_level, stream))

    try:
        if args.command == "migrate":
            print(json.dumps({"applied": asyncio.run(run_migrations(settings.database_url))}))
            return 0
        if settings.auto_migrate:
            asyncio.run(run_migrations(settings.database_url))
        if args.command in ("create-project", "create-key", "revoke-key"):
            print(json.dumps(asyncio.run(_admin_command(args, settings.database_url))))
            return 0
    except (MigrationError, CommandError) as e:
        print(f"trimset-server: {e}", file=sys.stderr)
        return 1

    if args.command == "worker":
        asyncio.run(_worker(settings))
        return 0

    uvicorn.run(
        "trimset_server.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_config=log_config(settings.log_level),
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""`trimset-server`: the commands that run on the server itself (ADR 0002)."""

import argparse
import asyncio
import json
import logging.config
import os
import signal
import sys
from collections.abc import Sequence

import uvicorn

from trimset_server.config import Settings
from trimset_server.logs import log_config
from trimset_server.migrate import MigrationError, run_migrations
from trimset_server.worker import run_worker


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
    return parser


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
    logging.config.dictConfig(log_config(settings.log_level))

    try:
        if args.command == "migrate" or settings.auto_migrate:
            applied = asyncio.run(run_migrations(settings.database_url))
            if args.command == "migrate":
                print(json.dumps({"applied": applied}))
                return 0
    except MigrationError as e:
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

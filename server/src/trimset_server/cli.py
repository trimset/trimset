"""`trimset-server`: the commands that run on the server itself (ADR 0002)."""

import argparse
import os
import sys
from collections.abc import Sequence

import uvicorn

from trimset_server.config import Settings
from trimset_server.logs import log_config


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trimset-server")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the API and the proxy")
    # All interfaces by default: the server runs in a container behind its port mapping.
    serve.add_argument("--host", default=os.environ.get("TRIMSET_HOST", "0.0.0.0"))  # noqa: S104
    serve.add_argument("--port", type=int, default=int(os.environ.get("TRIMSET_PORT", "8800")))
    serve.add_argument("--workers", type=int, default=int(os.environ.get("TRIMSET_WORKERS", "1")))
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ValueError as e:
        print(f"trimset-server: {e}", file=sys.stderr)
        return 2

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

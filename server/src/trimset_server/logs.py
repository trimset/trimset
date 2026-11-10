"""JSON logs on stdout (NFR-13), with trimset keys redacted (ADR 0011, NFR-11)."""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

# A key is its scope prefix plus 32 base62 characters (docs/03-api/README.md). Matching
# from 8 characters on also catches truncated keys. Proxy keys sit in URL paths, so
# access logs carry them unless they are redacted here.
#
# No left boundary on purpose: a key right after "_", a letter or "%2F" must still match.
# Redacting a little too much in a log is fine; leaking a key is not.
_KEY = re.compile(r"(ts[pira]_)[A-Za-z0-9]{8,}")


def redact(text: str) -> str:
    # Percent-decode first (repeatedly, for double encoding), so "tsp_%41…" or
    # "%74sp_…" can't slip past. The log line then shows the decoded form.
    for _ in range(3):
        if "%" not in text:
            break
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return _KEY.sub(r"\1[redacted]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False)


def log_config(level: str) -> dict[str, Any]:
    """A logging.config dictConfig for the app and uvicorn, all JSON on stdout."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": JsonFormatter}},
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "stream": "ext://sys.stdout",
            }
        },
        "loggers": {
            "uvicorn": {"handlers": ["stdout"], "level": level, "propagate": False},
            "uvicorn.access": {"handlers": ["stdout"], "level": level, "propagate": False},
        },
        "root": {"handlers": ["stdout"], "level": level},
    }

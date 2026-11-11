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
_MAX_DECODES = 8
_OVER_ENCODED = "[redacted: over-encoded]"


def redact(text: str) -> str:
    # Percent-decode first, so "tsp_%41…" or a prefix encoded several times can't slip past.
    # The log line then shows the decoded form. At most _MAX_DECODES passes, so crafted
    # input can't make every log line cost thousands of passes.
    for _ in range(_MAX_DECODES):
        if "%" not in text or (decoded := unquote(text)) == text:
            return _KEY.sub(r"\1[redacted]", text)
        text = decoded
    # Still encoded after that is no accident. Hide only the words that are still encoded,
    # so the rest of the line (client, method, status) stays in the log, and the marker
    # shows that something was hidden. One linear pass, no regex to backtrack.
    words = [_OVER_ENCODED if "%" in w and unquote(w) != w else w for w in text.split(" ")]
    return _KEY.sub(r"\1[redacted]", " ".join(words))


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


def log_config(level: str, stream: str = "ext://sys.stdout") -> dict[str, Any]:
    """A logging.config dictConfig for the app and uvicorn, all JSON on one stream.

    Long-running processes log to stdout (container convention). One-shot commands log to
    stderr, so their stdout holds nothing but the result a script reads.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": JsonFormatter}},
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "stream": stream,
            }
        },
        "loggers": {
            "uvicorn": {"handlers": ["stdout"], "level": level, "propagate": False},
            "uvicorn.access": {"handlers": ["stdout"], "level": level, "propagate": False},
        },
        "root": {"handlers": ["stdout"], "level": level},
    }

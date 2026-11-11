"""Request bodies, read with a limit enforced while reading, not after."""

import json
from typing import Any

from fastapi import Request


class TooLarge(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"The body is over {limit} bytes")
        self.limit = limit


async def read_body(request: Request, limit: int) -> bytes:
    """At most `limit` bytes; a bigger body is refused before it is all in memory."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise TooLarge(limit)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise TooLarge(limit)
    return bytes(body)


def parse_json(body: bytes) -> Any:
    """Strict JSON: refuses the NaN and Infinity that Python's parser would accept."""

    def no_constants(name: str) -> Any:
        raise ValueError(f"{name} is not JSON")

    return json.loads(body, parse_constant=no_constants)

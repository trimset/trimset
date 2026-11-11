"""Key checks for every route (FR-51, FR-52).

Each process remembers a key for at most `ttl_s` seconds before asking the database
again, so a revoked key is refused everywhere within that time (FR-52 allows 60 s).
Unknown keys are never cached, so the cache only ever holds real keys.
"""

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request

from trimset_server.errors import Problem
from trimset_server.keys import Principal, hash_key, looks_like_key

Loader = Callable[[bytes], Awaitable[Principal | None]]


class KeyCache:
    def __init__(
        self, loader: Loader, ttl_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._load, self._ttl, self._clock = loader, ttl_s, clock
        self._entries: dict[bytes, tuple[float, Principal]] = {}

    async def principal(self, key: str) -> Principal | None:
        if not looks_like_key(key):
            return None
        digest = hash_key(key)
        now = self._clock()
        hit = self._entries.get(digest)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        found = await self._load(digest)
        if found is None:
            self._entries.pop(digest, None)
        else:
            self._entries[digest] = (now, found)
        return found

    def forget(self, key_id: uuid.UUID) -> None:
        """Drop a key at once in this process (the others catch up within the TTL)."""
        for digest in [d for d, (_, p) in self._entries.items() if p.key_id == key_id]:
            del self._entries[digest]


def _bearer(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def require(capability: str) -> Callable[[Request], Awaitable[Principal]]:
    """A FastAPI dependency: the caller's Principal, if its key allows `capability`."""

    async def dependency(request: Request) -> Principal:
        key = _bearer(request)
        if key is None:
            raise Problem(
                401,
                "unauthorized",
                "A key is required",
                "Send Authorization: Bearer <key>.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        cache: KeyCache = request.app.state.keys
        principal = await cache.principal(key)
        if principal is None:
            raise Problem(
                401,
                "unauthorized",
                "Unknown or revoked key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not principal.can(capability):
            raise Problem(403, "forbidden", "This key's scope does not allow this")
        return principal

    return dependency

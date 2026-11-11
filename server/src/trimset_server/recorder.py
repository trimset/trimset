"""Proxy records, written off the request path (ADR 0011, NFR-06).

The proxy adds records to a bounded in-memory buffer and returns at once. A background
task writes them in batches. While the database is slow or down, records wait; when the
buffer is full, the oldest is dropped and counted. A response never waits for a write.
"""

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import asyncpg

from trimset_server.auth import DB_ERRORS
from trimset_server.records import RecordIn
from trimset_server.store import store

log = logging.getLogger("trimset.recorder")
# Errors meaning "this data will never go in", as opposed to "the database is unreachable".
_REFUSED = (asyncpg.DataError, asyncpg.IntegrityConstraintViolationError)


@dataclass(frozen=True)
class Pending:
    project_id: uuid.UUID
    record: RecordIn
    definition: dict[str, Any]
    name: str | None


class Recorder:
    def __init__(
        self,
        connect: Callable[[], Any],
        capacity: int,
        batch: int = 500,
        interval_s: float = 0.05,
        max_backoff_s: float = 5.0,
    ) -> None:
        self._connect = connect  # returns an async context manager yielding a connection
        self._queue: deque[Pending] = deque()
        self._capacity, self._batch = capacity, batch
        self._interval, self._max_backoff = interval_s, max_backoff_s
        self._wake = asyncio.Event()
        self.dropped = 0  # records lost because the buffer was full
        self.refused = 0  # records the database would not take

    @property
    def pending(self) -> int:
        return len(self._queue)

    def add(self, items: Iterable[Pending]) -> None:
        for item in items:
            if len(self._queue) >= self._capacity:
                self._queue.popleft()
                self.dropped += 1
                if self.dropped in (1, 10, 100) or self.dropped % 1000 == 0:
                    log.warning("record buffer full: %d records dropped so far", self.dropped)
            self._queue.append(item)
        if len(self._queue) >= self._batch:
            self._wake.set()

    async def _store(self, conn: Any, project_id: uuid.UUID, group: list[Pending]) -> None:
        definitions = {p.record.question_id: p.definition for p in group}
        names = {p.record.question_id: p.name for p in group if p.name}

        def refused(index: int) -> None:
            self.refused += 1

        await store(
            conn, project_id, list(enumerate(p.record for p in group)), definitions, names, refused
        )

    async def _store_group(self, conn: Any, project_id: uuid.UUID, group: list[Pending]) -> None:
        """One project's records. If the database refuses the group, each record is tried
        alone, so only the ones it won't take are dropped (and counted)."""
        try:
            await self._store(conn, project_id, group)
            return
        except _REFUSED as e:
            log.warning(
                "project %s: retrying %d records one by one (%s)",
                project_id,
                len(group),
                type(e).__name__,
            )
        for item in group:
            try:
                await self._store(conn, project_id, [item])
            except _REFUSED:
                self.refused += 1

    async def flush_once(self) -> bool:
        """Write up to one batch, project by project. False if the database was
        unreachable; the projects not yet written go back to the front of the queue."""
        items = [self._queue.popleft() for _ in range(min(self._batch, len(self._queue)))]
        if not items:
            return True
        groups: dict[uuid.UUID, list[Pending]] = {}
        for item in items:
            groups.setdefault(item.project_id, []).append(item)
        pending = list(groups.items())
        try:
            async with self._connect() as conn:
                while pending:
                    project_id, group = pending[0]
                    await self._store_group(conn, project_id, group)
                    pending.pop(0)
        except DB_ERRORS as e:
            retry = [item for _, group in pending for item in group]
            self._queue.extendleft(reversed(retry))  # back to the front, in order
            log.warning("could not write %d records (%s); will retry", len(retry), type(e).__name__)
            return False
        return True

    async def run(self, stop: asyncio.Event) -> None:
        backoff = self._interval
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=backoff)
            self._wake.clear()
            ok = await self.flush_once()
            backoff = (
                self._interval if ok else min(backoff * 2 or self._interval, self._max_backoff)
            )

    async def drain(self, timeout_s: float) -> None:
        """At shutdown: write what's left, for at most `timeout_s`."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_s):
                while self._queue and await self.flush_once():
                    pass
        if self._queue:
            log.warning("shutting down with %d records unwritten", len(self._queue))

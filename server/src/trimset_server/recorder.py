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

    async def _write(self, items: list[Pending]) -> None:
        by_project: dict[uuid.UUID, list[Pending]] = {}
        for item in items:
            by_project.setdefault(item.project_id, []).append(item)
        async with self._connect() as conn:
            for project_id, group in by_project.items():
                definitions = {p.record.question_id: p.definition for p in group}
                names = {p.record.question_id: p.name for p in group if p.name}

                def refused(index: int) -> None:
                    self.refused += 1

                await store(
                    conn,
                    project_id,
                    list(enumerate(p.record for p in group)),
                    definitions,
                    names,
                    refused,
                )

    async def flush_once(self) -> bool:
        """Write up to one batch. False if the database refused the attempt."""
        items = [self._queue.popleft() for _ in range(min(self._batch, len(self._queue)))]
        if not items:
            return True
        try:
            await self._write(items)
        except (asyncpg.DataError, asyncpg.IntegrityConstraintViolationError) as e:
            # Data the database will never take: retrying would block the buffer for good.
            self.refused += len(items)
            log.warning(
                "dropped %d records the database refused (%s)", len(items), type(e).__name__
            )
            return True
        except DB_ERRORS as e:
            self._queue.extendleft(reversed(items))  # back to the front, in order
            log.warning("could not write %d records (%s); will retry", len(items), type(e).__name__)
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

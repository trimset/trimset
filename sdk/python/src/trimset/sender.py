"""Sending records, action reports and outcomes (sdk.md §5).

One background thread and one bounded queue per kind. When a queue is full, its oldest
item is dropped and counted; each log batch reports the count. A batch goes when it holds
`batch_size` items or `flush_interval` has passed. Network errors, 429 and 5xx keep the
batch and retry with exponential backoff and full jitter; any other 4xx drops it, with
one warning per status. Nothing logged here holds a state, a key or a tag value.
"""

import collections
import json
import logging
import os
import random
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from importlib.metadata import version
from typing import Any

log = logging.getLogger("trimset")
ROUTES = {
    "records": "/v1/decisions:log",
    "actions": "/v1/decisions:actions",
    "outcomes": "/v1/outcomes",
}
MAX_BATCH = 1000  # the API's limit per request
BACKOFF = (0.5, 60.0)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a redirect would carry the key elsewhere


_open = urllib.request.build_opener(_NoRedirect).open


class _Retry(Exception):
    pass


class Sender:
    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        timeout: float = 5.0,
        buffer_size: int = 10_000,
        batch_size: int = 100,
        flush_interval: float = 1.0,
    ) -> None:
        self.base_url, self._key, self.timeout = base_url, key, timeout
        self.buffer_size = buffer_size
        self.batch_size = max(1, min(batch_size, MAX_BATCH))
        self.flush_interval = flush_interval
        self._queues: dict[str, collections.deque[Any]] = {k: collections.deque() for k in ROUTES}
        self._questions: dict[str, Any] = {}  # definitions the server may not have yet
        self._acked: set[str] = set()
        self._dropped = 0  # records dropped since the last log batch reported it
        self.dropped_total = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._stop = threading.Event()
        self._failures = 0
        self._warned: set[str] = set()
        self._pid: int | None = None
        self._thread: threading.Thread | None = None
        self._client = {"sdk": "trimset-python", "version": version("trimset")}

    # ------------------------------------------------------------ the calling side

    def add(
        self, kind: str, items: Iterable[Any], questions: Mapping[str, Any] | None = None
    ) -> None:
        """Queues items without blocking; when the queue is full the oldest goes."""
        with self._lock:
            queue = self._queues[kind]
            for item in items:
                if len(queue) >= self.buffer_size:
                    queue.popleft()
                    self.dropped_total += 1
                    if kind == "records":
                        self._dropped += 1
                queue.append(item)
            for qid, definition in (questions or {}).items():
                if qid not in self._acked:
                    self._questions[qid] = definition
            self._idle.clear()
            if len(queue) >= self.batch_size:
                self._wake.set()
        self.ensure_thread()

    def flush(self, timeout: float = 2.0) -> bool:
        """Sends what is queued now; True when everything went within `timeout`."""
        self.ensure_thread()
        self._wake.set()
        return self._idle.wait(timeout)

    def close(self, timeout: float = 2.0) -> None:
        self.flush(timeout)
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._pid == os.getpid():
            self._thread.join(timeout=1.0)

    def ensure_thread(self) -> None:
        """Starts the thread, and starts it again in a forked child."""
        if self._pid == os.getpid() or self._stop.is_set():
            return
        self._pid = os.getpid()
        self._thread = threading.Thread(target=self._run, name="trimset-send", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ the thread

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.flush_interval)
            self._wake.clear()
            try:
                self._drain()
                self._failures = 0
            except _Retry:
                self._failures += 1
                delay = random.uniform(0, min(BACKOFF[1], BACKOFF[0] * 2**self._failures))  # noqa: S311
                self._stop.wait(delay)
            except Exception:  # a bug here must never stop the thread
                log.exception("trimset: sending failed")

    def _drain(self) -> None:
        for kind in ROUTES:
            while True:
                with self._lock:
                    queue = self._queues[kind]
                    batch = [queue.popleft() for _ in range(min(len(queue), self.batch_size))]
                if not batch:
                    break
                try:
                    self._send(kind, batch)
                except _Retry:
                    with self._lock:  # back to the front, in order
                        self._queues[kind].extendleft(reversed(batch))
                    raise
        with self._lock:
            if not any(self._queues.values()):
                self._idle.set()

    def _send(self, kind: str, batch: list[Any]) -> None:
        body: dict[str, Any] = {kind: batch}
        sent_questions: list[str] = []
        if kind == "records":
            with self._lock:
                wanted = {r.get("question_id") for r in batch}
                questions = {q: d for q, d in self._questions.items() if q in wanted}
                body["client"] = self._client | {"dropped_since_last": self._dropped}
                dropped = self._dropped
            if questions:
                body["questions"] = questions
                sent_questions = list(questions)
        status, result = self._post(ROUTES[kind], body)
        if status in (200, 207):
            if kind == "records":
                with self._lock:
                    self._dropped -= dropped
                    for qid in sent_questions:
                        self._acked.add(qid)
                        self._questions.pop(qid, None)
            rejected = (result or {}).get("rejected", 0)
            if rejected:
                where = [e.get("pointer", "") for e in (result or {}).get("errors", [])[:3]]
                self._warn(
                    f"{kind}-{status}", f"trimset: the server rejected {rejected} {kind} {where}"
                )
            return
        self.dropped_total += len(batch)
        self._warn(f"{kind}-{status}", f"trimset: dropped {len(batch)} {kind}: HTTP {status}")

    def _post(self, path: str, body: Any) -> tuple[int, Any]:
        request = urllib.request.Request(  # noqa: S310 - http(s) only, checked by the Client
            self.base_url + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "User-Agent": "trimset-python",
            },
        )
        try:
            with _open(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                raise _Retry from None
            return e.code, None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            raise _Retry from None

    def _warn(self, once: str, message: str) -> None:
        if once not in self._warned:
            self._warned.add(once)
            log.warning(message)

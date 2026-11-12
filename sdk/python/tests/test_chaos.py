"""NFR-07: the SDK never raises into app code, never blocks on the network, and holds a
bounded number of records in memory. Here against a server that hangs, one that sends
garbage, arbitrary arguments, and a fork. The same promises against a real server that is
stopped and started are in server/tests/test_sdk_integration.py."""

import json
import os
import signal
import socket
import threading
import time
import uuid
import warnings
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from trimset import Client
from trimset.client import Snapshot, build_snapshot

GATES = json.loads(
    (Path(__file__).parents[3] / "docs" / "03-api" / "vectors" / "gates.json").read_text()
)
BUNDLE = GATES["bundle"]
MODEL = "jev-1.14.0"
PLAIN = GATES["examples"][0]["gate"]["decision_id"]  # a decision id the 1% audit skips
REQUEST = {
    "model": "jev-latest",
    "state": {"refund_id": "rf-1"},
    "questions": {"fraud": {"type": "noul", "instructions": "Is this fraud?"}},
}
RESPONSE = {"model": MODEL, "answers": {"fraud": {"type": "noul", "noul": 0.4}}}


def _cached(url: str = "http://127.0.0.1:1", **options: Any) -> Client:
    """A client holding the reference bundle, as if it had polled once."""
    options.setdefault("poll_interval", 3600)
    client = Client(url, "tsi_test", **GATES["client"], **options)
    client._snapshot = build_snapshot(BUNDLE, Snapshot(), time.time())
    return client


def _offline() -> Client:
    client = _cached()
    client.close(0)  # no threads, so no network at all
    return client


OFFLINE = _offline()


@pytest.mark.req("NFR-07")
def test_the_reporting_calls_never_raise_either() -> None:
    client = OFFLINE
    junk: list[Any] = [None, 1, "x", [], {}, {"model": 1}, object(), float("nan")]
    for a in junk:
        for b in junk:
            assert client.log_exchange(a, b, format="systemone") == []
            client.log(a)  # type: ignore[arg-type]
            client.outcome(decision_id=a, label=b, source="downstream")
            client.outcome(external_ref=a, question=b, label=a, source=b)
            client.instrument(lambda r: r, model="m")(a)


@pytest.fixture
def hung() -> Iterator[str]:
    """A server that accepts connections and never answers."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    listener.close()


@pytest.mark.req("NFR-07")
def test_a_server_that_never_answers_never_blocks_the_app(hung: str) -> None:
    client = _cached(hung, timeout=0.3, flush_interval=0.05, buffer_size=50)
    start = time.perf_counter()
    for _ in range(1500):
        did = str(uuid.uuid4())
        d = client.gate("refund_fraud", p=0.5, model_served=MODEL, decision_id=did)
        assert d.source in ("policy", "audit")  # answered from the cache; 1% are audited
        client.outcome(decision_id=did, label=True, source="downstream")
        client.log_exchange(REQUEST, RESPONSE, format="systemone")
    # Every call that waited on the network would cost 0.3 s: this would take minutes.
    assert time.perf_counter() - start < 5.0

    queues = client._sender._queues  # bounded: the oldest are dropped, and counted
    assert all(len(q) <= 50 for q in queues.values())
    assert client.dropped > 0

    start = time.perf_counter()
    assert client.flush(0.2) is False  # can't deliver, and says so within its timeout
    client.close(0.2)
    assert time.perf_counter() - start < 2.0


def _serve(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    return http


@pytest.mark.req("NFR-07")
def test_garbage_from_the_server_keeps_the_last_good_policies() -> None:
    answers: list[tuple[int, bytes]] = [(200, json.dumps(BUNDLE).encode())]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, body = answers[0]
            self.send_response(status)
            if status == 200 and body.startswith(b"{"):
                self.send_header("ETag", f'"{BUNDLE["etag"]}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    http = _serve(Handler)
    try:
        client = Client(
            f"http://127.0.0.1:{http.server_port}", "tsi_test", **GATES["client"],
            poll_interval=0.05, poll_jitter=0.0,
        )  # fmt: skip
        assert client.wait_for_policies(5)
        good = client.snapshot
        for garbage in [
            (200, b"<html>maintenance</html>"),
            (200, b'{"schema": "bundle/9"}'),
            (200, b'{"schema": "bundle/1", "questions": 7, "policies": []}'),
            (200, b""),
            (500, b"boom"),
            (401, b"{}"),
        ]:
            answers[0] = garbage
            time.sleep(0.4)  # several polls, and the backoff between them
            assert client._thread is not None and client._thread.is_alive()
            d = client.gate("refund_fraud", p=0.83, model_served=MODEL, decision_id=PLAIN)
            assert (d.source, d.policy_version) == ("policy", 7), garbage
            assert client.snapshot.policies == good.policies
        client.close(0)
    finally:
        http.shutdown()


@pytest.mark.req("NFR-07")
@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_forked_child_starts_its_own_threads() -> None:
    """gunicorn --preload: the master creates the client, the workers fork from it."""
    client = _cached(flush_interval=0.05)
    r, w = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # fork() in a threaded process
        pid = os.fork()
    if pid == 0:  # the child: report what it sees, and never return into pytest
        code = 1
        try:
            signal.alarm(20)
            parent_poller = client._thread
            d = client.gate("refund_fraud", p=0.83, model_served=MODEL, decision_id=PLAIN)
            ok = (
                d.source == "policy"
                and client._pid == os.getpid()
                and client._thread is not parent_poller
                and client._thread is not None
                and client._thread.is_alive()
                and client._sender._thread is not None
                and client._sender._thread.is_alive()
            )
            code = 0 if ok else 1
        finally:
            os.write(w, b"x")
            os._exit(code)
    os.close(w)
    _, status = os.waitpid(pid, 0)
    os.close(r)
    assert os.waitstatus_to_exitcode(status) == 0
    client.close(0)

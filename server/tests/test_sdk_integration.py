"""The Python SDK against a real server on a real port, and a real Postgres (CI).

NFR-05: an activation or a rollback reaches a running SDK. NFR-07: the SDK never raises or
blocks while the server is down, keeps deciding from its cache, and delivers what it
queued when the server returns. (The same promises against a hanging server, garbage and a
fork, without a database, are in sdk/python/tests/test_chaos.py.)"""

import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
import uvicorn
from fastapi.testclient import TestClient

from trimset import Client
from trimset_server import jobs, keys
from trimset_server.app import create_app
from trimset_server.config import Settings
from trimset_server.migrate import run_migrations
from trimset_tools.datagen import generate, send

pytestmark = [pytest.mark.anyio, pytest.mark.integration]
MODEL = "jev-1.14.0"
POLL_S, JITTER = 0.3, 0.2


class Live:
    """The server on a real port, in a thread, that can be stopped and started again."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        app = create_app(Settings(database_url=self.dsn, secret=b"s" * 32))
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        _wait(lambda: self.server is not None and self.server.started, 20, "the server to start")

    def stop(self) -> None:
        if self.server is not None and self.thread is not None:
            self.server.should_exit = True
            self.thread.join(15)
            self.server = self.thread = None


def _wait(condition: Callable[[], Any], seconds: float, what: str) -> float:
    """Seconds until `condition` held; fails after `seconds`."""
    start = time.perf_counter()
    while not condition():
        if time.perf_counter() - start > seconds:
            raise AssertionError(f"timed out after {seconds} s waiting for {what}")
        time.sleep(0.02)
    return time.perf_counter() - start


async def _db(dsn: str, fn: Callable[..., Any]) -> Any:
    conn = await asyncpg.connect(dsn)
    try:
        return await fn(conn)
    finally:
        await conn.close()


async def _project(dsn: str) -> tuple[str, str]:
    """A project with triage history and a fit; returns its ingest and admin keys."""
    await run_migrations(dsn)

    async def made(conn: Any) -> tuple[str, str]:
        pid = await keys.create_project(conn, "demo")
        ingest = (await keys.create_key(conn, pid, "ingest", "app"))[1]
        return ingest, (await keys.create_key(conn, pid, "admin", "ops"))[1]

    ingest, admin = await _db(dsn, made)
    auth = {"Authorization": f"Bearer {admin}"}
    now = datetime.now(UTC)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        send(generate("ticket-triage", seed=2, end=now, days=10, per_day=200), c, ingest, now=now)
        horizon = {"settings": {"label_horizon_days": 3}}
        assert c.patch("/v1/admin/questions/team", json=horizon, headers=auth).status_code == 200
        r = c.post("/v1/admin/questions/team/fits", json={"model_served": MODEL}, headers=auth)
        assert r.status_code == 202
    await _db(dsn, jobs.work)
    return ingest, admin


async def _activate(c: TestClient, dsn: str, auth: dict[str, str], alpha: float) -> str:
    """Compiles a policy for the team question and takes it live; returns its id."""
    target = {"kind": "risk", "metric": "error_budget", "alpha": alpha}
    body = {"model_served": MODEL, "target": target}
    r = c.post("/v1/admin/questions/team/compile", json=body, headers=auth)
    assert r.status_code == 202, r.json()
    await _db(dsn, jobs.work)
    done = c.get(r.headers["location"], headers=auth).json()
    assert "policy_id" in (done["result"] or {}), done
    policy_id: str = done["result"]["policy_id"]
    for action in ("submit", "approve", "activate"):
        step = {"action": action}
        moved = c.post(f"/v1/admin/policies/{policy_id}/transitions", json=step, headers=auth)
        assert moved.status_code == 200, moved.json()
    return policy_id


def _gate(sdk: Client) -> int | None:
    """The version of the policy the SDK would apply to a team question right now."""
    options = sdk.snapshot.questions["team"]["definition"]["options"]
    probs = [0.7] + [0.3 / (len(options) - 1)] * (len(options) - 1)
    return sdk.gate("team", probabilities=probs, model_served=MODEL).policy_version


@pytest.mark.req("NFR-05")
@pytest.mark.req("FR-29")
async def test_an_activation_and_a_rollback_reach_a_running_sdk(dsn: str) -> None:
    ingest, admin = await _project(dsn)
    auth = {"Authorization": f"Bearer {admin}"}
    live = Live(dsn)
    live.start()
    sdk = Client(live.url, ingest, poll_interval=POLL_S, poll_jitter=JITTER, flush_interval=0.05)
    try:
        with TestClient(create_app(Settings(database_url=dsn))) as c:
            await _activate(c, dsn, auth, 0.4)
            assert sdk.wait_for_policies(10)
            _wait(lambda: _gate(sdk) == 1, 10, "v1 to reach the SDK")

            v2 = await _activate(c, dsn, auth, 0.35)  # retires v1
            activated = _wait(lambda: _gate(sdk) == 2, 10, "v2 to reach the SDK")

            rollback = {"model_served": MODEL, "reason": "v2 escalates too much"}
            r = c.post("/v1/admin/questions/team/rollback", json=rollback, headers=auth)
            assert r.status_code == 200 and r.json()["policy"]["version"] == 1, r.json()
            rolled_back = _wait(lambda: _gate(sdk) == 1, 10, "the rollback to reach the SDK")

            # At the default 30 s poll it is at most 36 s plus a request, inside the 60 s bound.
            bound = POLL_S * (1 + JITTER) + 1.0
            print(
                f"\nactivation {activated:.2f} s, rollback {rolled_back:.2f} s (bound {bound:.1f})"
            )
            assert activated < bound and rolled_back < bound, (activated, rolled_back)
            assert v2 != r.json()["policy"]["policy_id"]
    finally:
        sdk.close(0)
        live.stop()


@pytest.mark.req("NFR-07")
async def test_the_sdk_keeps_deciding_while_the_server_is_down_and_catches_up(dsn: str) -> None:
    ingest, admin = await _project(dsn)
    auth = {"Authorization": f"Bearer {admin}"}
    live = Live(dsn)
    live.start()
    sdk = Client(
        live.url, ingest, poll_interval=POLL_S, poll_jitter=JITTER, flush_interval=0.05, timeout=1.0
    )
    request = {
        "model": "jev-requested",
        "state": {"ticket": "t-1"},
        "questions": {"chaos": {"type": "noul", "instructions": "Is this a chaos test?"}},
    }
    response = {"model": "jev-chaos-1", "answers": {"chaos": {"type": "noul", "noul": 0.3}}}
    sent = 200
    try:
        with TestClient(create_app(Settings(database_url=dsn))) as c:
            await _activate(c, dsn, auth, 0.4)
            assert sdk.wait_for_policies(10)
            _wait(lambda: _gate(sdk) == 1, 10, "v1 to reach the SDK")

            live.stop()  # the server goes away

            start = time.perf_counter()
            for i in range(sent):
                assert _gate(sdk) == 1  # decided from the cache, nothing raised or waited
                sdk.log_exchange(request, response, format="systemone", external_ref=f"t-{i}")
                sdk.outcome(
                    external_ref=f"t-{i}", question="chaos", label=True, source="downstream"
                )
            assert time.perf_counter() - start < 5.0
            assert sdk.flush(0.3) is False  # nowhere to send it yet
            assert sdk.dropped == 0  # and nothing lost: the queue holds 10,000

            v2 = await _activate(c, dsn, auth, 0.35)  # an operator moves on in the meantime

            live.start()  # the server returns, on the same port
            assert sdk.flush(30), "the queued records were never delivered"
            _wait(lambda: _gate(sdk) == 2, 30, "the SDK to catch up with v2")

            def stored() -> int:
                r = c.get(
                    "/v1/admin/decisions", params={"question": "chaos", "limit": 1000}, headers=auth
                )
                return len(r.json()["items"])

            _wait(lambda: stored() == sent, 15, f"{sent} decisions to be stored")
            assert sdk.dropped == 0
            assert v2
    finally:
        sdk.close(0)
        live.stop()

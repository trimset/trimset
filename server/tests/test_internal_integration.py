"""The Rust proxy's private interface (internal.py) against a real Postgres (CI)."""

import json
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from trimset.wire import uuid7
from trimset_server import keys
from trimset_server.app import create_app
from trimset_server.config import Settings
from trimset_server.internal import internal_token
from trimset_server.migrate import run_migrations
from trimset_server.upstreams import Upstream

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

SECRET = b"internal-test-secret-0123456789abcdef"
TOKEN = {"X-Trimset-Internal": internal_token(SECRET)}
REQUEST = {
    "model": "jev-latest",
    "state": {"refund_id": "rf-1"},
    "questions": {
        "fraud": {"type": "noul", "instructions": "Is this refund request fraudulent?"},
        "team": {
            "type": "choice",
            "instructions": "Which team?",
            "criteria": {"a": None, "b": None},
        },
    },
}
ANSWER = {
    "model": "jev-1.14.0",
    "answers": {
        "fraud": {"type": "noul", "noul": 0.83},
        "team": {"type": "choice", "probabilities": {"a": 0.8, "b": 0.2}},
    },
}


async def _setup(dsn: str) -> tuple[Any, dict[str, str]]:
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        pid = await keys.create_project(conn, "acme")
        made = {s: (await keys.create_key(conn, pid, s, s))[1] for s in keys.PREFIXES}  # type: ignore[arg-type]
        return pid, made
    finally:
        await conn.close()


def _app(dsn: str) -> Any:
    upstreams = {"mock": Upstream("mock", "http://mock.test", "systemone")}
    return create_app(Settings(database_url=dsn, secret=SECRET, upstreams=upstreams))


async def test_without_the_token_there_is_nothing_there(dsn: str) -> None:
    await run_migrations(dsn)
    with TestClient(_app(dsn)) as c:
        for headers in ({}, {"X-Trimset-Internal": "0" * 64}):
            r = c.post("/internal/proxy/principal", json={"key": "x"}, headers=headers)
            assert r.status_code == 404
            r = c.post("/internal/proxy/exchanges", json={"exchanges": []}, headers=headers)
            assert r.status_code == 404


async def test_a_key_is_looked_up_for_the_proxy(dsn: str) -> None:
    pid, made = await _setup(dsn)
    with TestClient(_app(dsn)) as c:

        def ask(key: str) -> Any:
            return c.post("/internal/proxy/principal", json={"key": key}, headers=TOKEN).json()

        found = ask(made["proxy"])["principal"]
        assert found["project_id"] == str(pid)
        assert found["upstream"] == {
            "name": "mock",
            "url": "http://mock.test",
            "format": "systemone",
            "server_key": False,
            "timeout_ms": 10_000,
        }
        assert found["max_body_bytes"] == 8 * 1024 * 1024
        assert "principal" in ask(made["admin"])  # admin and ingest keys may proxy too
        assert "principal" in ask(made["ingest"])
        assert ask(made["review"])["denied"]["status"] == 403
        assert ask("tsp_" + "x" * 32)["denied"] == {
            "status": 401,
            "code": "invalid_key",
            "message": "Unknown or revoked trimset key",
        }
        assert ask("not a key")["denied"]["code"] == "invalid_key"


async def test_exchanges_are_recorded_with_the_proxys_ids(dsn: str) -> None:
    pid, _ = await _setup(dsn)
    ids = [uuid7(), uuid7()]
    exchange = {
        "project_id": str(pid),
        "format": "systemone",
        "request": json.dumps(REQUEST),
        "response": json.dumps(ANSWER),
        "decision_ids": ids,
        "latency_ms": 12.5,
        "ts_ms": int(datetime.now(UTC).timestamp() * 1000),
        "external_ref": "rf-1",
        "tags": "channel=web",
    }
    short = exchange | {"decision_ids": [uuid7()]}  # fewer ids than questions
    with TestClient(_app(dsn)) as c:
        r = c.post(
            "/internal/proxy/exchanges", json={"exchanges": [exchange, short]}, headers=TOKEN
        )
        assert (r.status_code, r.json()) == (202, {"queued": 1})
    # Leaving the client drains the recorder.

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT decision_id::text AS id, external_ref, tags, latency_ms, model_served "
            "FROM decisions ORDER BY type DESC"
        )
    finally:
        await conn.close()
    assert [r["id"] for r in rows] == ids  # noul first, then choice: question order
    assert {r["external_ref"] for r in rows} == {"rf-1"}
    assert json.loads(rows[0]["tags"]) == {"channel": "web"}
    assert rows[0]["latency_ms"] == pytest.approx(12.5)
    assert rows[0]["model_served"] == "jev-1.14.0"


@pytest.mark.req("FR-09")
async def test_the_tag_allow_list_holds_for_proxied_exchanges(dsn: str) -> None:
    pid, _ = await _setup(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "UPDATE projects SET settings = $2::jsonb WHERE project_id = $1",
            pid,
            json.dumps({"privacy": {"tag_allowlist": ["channel"]}}),
        )
    finally:
        await conn.close()
    exchange = {
        "project_id": str(pid),
        "format": "systemone",
        "request": json.dumps(REQUEST),
        "response": json.dumps(ANSWER),
        "decision_ids": [uuid7(), uuid7()],
        "latency_ms": 1.0,
        "ts_ms": int(datetime.now(UTC).timestamp() * 1000),
        "tags": "channel=web,customer_email=ann%40example.com",
    }
    with TestClient(_app(dsn)) as c:
        r = c.post("/internal/proxy/exchanges", json={"exchanges": [exchange]}, headers=TOKEN)
        assert r.json() == {"queued": 1}
    conn = await asyncpg.connect(dsn)
    try:
        tags = await conn.fetch("SELECT tags FROM decisions")
    finally:
        await conn.close()
    assert [json.loads(t["tags"]) for t in tags] == [{"channel": "web"}] * 2

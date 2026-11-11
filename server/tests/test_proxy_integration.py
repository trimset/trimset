"""The proxy and project settings against a real Postgres (CI)."""

import json
from typing import Any

import asyncpg
import httpx2
import pytest
from fastapi.testclient import TestClient

from trimset_server import keys
from trimset_server.app import create_app
from trimset_server.config import Settings
from trimset_server.migrate import run_migrations
from trimset_server.upstreams import Upstream

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

SECRET = b"proxy-integration-secret-0123456789"
REQUEST = {
    "model": "jev-latest",
    "state": {"refund_id": "rf-1001"},
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


async def _keys(dsn: str) -> dict[str, str]:
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        pid = await keys.create_project(conn, "acme")
        return {
            s: (await keys.create_key(conn, pid, s, s))[1] for s in ("proxy", "ingest", "admin")
        }  # type: ignore[arg-type]
    finally:
        await conn.close()


def _app(dsn: str, **upstreams: Upstream) -> Any:
    upstreams = upstreams or {"jev": Upstream("jev", "https://jev.test", "systemone", key="sk-op")}
    fake = httpx2.MockTransport(lambda r: httpx2.Response(200, json=ANSWER))
    return create_app(
        Settings(database_url=dsn, secret=SECRET, upstreams=upstreams, key_cache_ttl_s=0),
        transport=fake,
    )


async def _rows(dsn: str, sql: str, *args: Any) -> list[Any]:
    conn = await asyncpg.connect(dsn)
    try:
        return list(await conn.fetch(sql, *args))
    finally:
        await conn.close()


@pytest.mark.req("FR-04")
@pytest.mark.req("FR-07")
async def test_proxied_decisions_reach_the_database(dsn: str) -> None:
    k = await _keys(dsn)
    with TestClient(_app(dsn)) as c:
        r = c.post(
            f"/p/{k['proxy']}/v1/systemone",
            json=REQUEST,
            headers={"X-Trimset-External-Ref": "rf-1001", "X-Trimset-Tags": "channel=web"},
        )
        assert r.status_code == 200
        ids = r.headers["x-trimset-decision-id"].split(",")
    # Leaving the client shuts the app down; the recorder writes what it still holds.

    rows = await _rows(
        dsn, "SELECT * FROM decisions WHERE decision_id = ANY($1::uuid[]) ORDER BY type", ids
    )
    assert [r["type"] for r in rows] == ["choice", "noul"]
    noul = rows[1]
    assert noul["probs"] == pytest.approx([0.83])
    assert (noul["external_ref"], json.loads(noul["tags"])) == ("rf-1001", {"channel": "web"})
    assert noul["state_hash"] and len(noul["state_hash"]) == 64
    assert noul["format"] == "systemone"

    names = await _rows(dsn, "SELECT name FROM questions ORDER BY name")
    assert [n["name"] for n in names] == ["fraud", "team"]
    models = await _rows(dsn, "SELECT model_requested, model_served FROM model_versions")
    assert [tuple(m) for m in models] == [("jev-latest", "jev-1.14.0")]


async def test_project_settings_read_and_merge_patch(dsn: str) -> None:
    k = await _keys(dsn)
    upstreams = {
        "jev": Upstream("jev", "https://jev.test", "systemone", key="sk-op"),
        "ollaya": Upstream("ollaya", "http://ollaya:11435", "systemone"),
    }
    admin = {"Authorization": f"Bearer {k['admin']}"}
    with TestClient(_app(dsn, **upstreams)) as c:
        view = c.get("/v1/admin/project", headers=admin).json()
        assert view["name"] == "acme"
        assert view["settings"]["retention_days"] == 400
        assert view["available"]["upstreams"] == [
            {"name": "jev", "format": "systemone", "server_key": True},
            {"name": "ollaya", "format": "systemone", "server_key": False},
        ]

        patch = {
            "upstream": {"name": "jev", "credentials": "server"},
            "prices": {"jev-1.14.0": {"input": 0.042}},
        }
        r = c.patch("/v1/admin/project", json=patch, headers=admin)
        assert r.status_code == 200
        assert r.json()["settings"]["upstream"]["credentials"] == "server"

        for bad in (
            {"upstream": {"name": "nowhere"}},
            {"upstream": {"name": "ollaya", "credentials": "server"}},  # no operator key
            {"privacy": {"store_raw_state": True}},  # not built yet
            {"retention_days": "400"},
        ):
            assert c.patch("/v1/admin/project", json=bad, headers=admin).status_code == 422

        r = c.patch("/v1/admin/project", json={"prices": None}, headers=admin)
        assert r.json()["settings"]["prices"] == {}

        ingest = {"Authorization": f"Bearer {k['ingest']}"}
        assert c.patch("/v1/admin/project", json={}, headers=ingest).status_code == 403

    (stored,) = await _rows(dsn, "SELECT settings FROM projects")
    assert json.loads(stored["settings"]) == {"upstream": {"name": "jev", "credentials": "server"}}

"""The server against its contract, docs/03-api/openapi.yaml (T1.12).

Every route the app serves is in the contract. Every response below uses a status its
operation lists, with a body that matches the listed schema. An error an operation
doesn't list must still be a Problem (RFC 9457). The proxy's routes are Rust's, tested in
proxy/src/main.rs; this file covers the Python server.
"""

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from trimset_server import keys
from trimset_server.app import create_app
from trimset_server.config import Settings
from trimset_server.migrate import run_migrations
from trimset_server.upstreams import Upstream
from trimset_tools.datagen import generate

API = Path(__file__).resolve().parents[2] / "docs" / "03-api"
SPEC = yaml.safe_load((API / "openapi.yaml").read_text())
PARAM = re.compile(r"\{[^}]+\}")


def test_every_route_the_app_serves_is_in_the_contract() -> None:
    app = create_app(Settings(database_url="postgresql://unused"))
    served = {
        (method, PARAM.sub("{}", route.path))
        for route in app.routes
        if isinstance(route, APIRoute) and not route.path.startswith("/internal/")
        for method in route.methods
    }  # /internal/* is the Rust proxy's private interface, refused from outside
    listed = {
        (method.upper(), PARAM.sub("{}", path))
        for path, ops in SPEC["paths"].items()
        for method in ops
        if method in ("get", "post", "put", "patch", "delete")
    }
    assert served - listed == set()


async def _keys(dsn: str) -> dict[str, str]:
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        pid = await keys.create_project(conn, "contract")
        return {s: (await keys.create_key(conn, pid, s, s))[1] for s in keys.PREFIXES}  # type: ignore[arg-type]
    finally:
        await conn.close()


def _client(dsn: str) -> TestClient:
    upstreams = {"mock": Upstream("mock", "http://mock.test", "systemone")}
    return TestClient(create_app(Settings(database_url=dsn, upstreams=upstreams)))


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.anyio
@pytest.mark.integration
async def test_ops_admin_and_errors_match_the_contract(dsn: str, check: Any) -> None:
    k = await _keys(dsn)
    admin = _auth(k["admin"])
    with _client(dsn) as c:
        check(c.get("/health"), "GET", "/health")
        check(c.get("/ready"), "GET", "/ready")

        check(c.get("/v1/admin/keys"), "GET", "/v1/admin/keys")  # 401, a Problem
        check(c.get("/v1/admin/keys", headers=_auth(k["ingest"])), "GET", "/v1/admin/keys")  # 403
        check(c.get("/v1/admin/keys", headers=admin), "GET", "/v1/admin/keys")
        created = c.post("/v1/admin/keys", json={"scope": "ingest", "label": "ci"}, headers=admin)
        check(created, "POST", "/v1/admin/keys")
        bad = c.post("/v1/admin/keys", json={"scope": "root", "label": "ci"}, headers=admin)
        check(bad, "POST", "/v1/admin/keys")
        gone = c.delete(f"/v1/admin/keys/{created.json()['key_id']}", headers=admin)
        check(gone, "DELETE", "/v1/admin/keys/{key_id}")
        check(
            c.delete(f"/v1/admin/keys/{uuid.uuid4()}", headers=admin),
            "DELETE",
            "/v1/admin/keys/{key_id}",
        )

        check(c.get("/v1/admin/project", headers=admin), "GET", "/v1/admin/project")
        patch = {"prices": {"jev-1.14.0": {"input": 0.04}}, "upstream": {"name": "mock"}}
        check(c.patch("/v1/admin/project", json=patch, headers=admin), "PATCH", "/v1/admin/project")
        check(
            c.patch("/v1/admin/project", json={"retention_days": 0}, headers=admin),
            "PATCH",
            "/v1/admin/project",
        )


@pytest.mark.anyio
@pytest.mark.integration
async def test_ingest_outcomes_actions_and_questions_match_the_contract(
    dsn: str, check: Any
) -> None:
    k = await _keys(dsn)
    ingest, admin = _auth(k["ingest"]), _auth(k["admin"])
    data = generate("refund-fraud", seed=1, end=datetime.now(UTC), days=200, per_day=5)
    body = {"questions": {data.question_id: data.definition}, "records": data.records}
    with _client(dsn) as c:
        check(c.post("/v1/decisions:log", json=body, headers=ingest), "POST", "/v1/decisions:log")
        bad = body | {"records": [*data.records[:1], {"schema": "record/1"}]}
        check(c.post("/v1/decisions:log", json=bad, headers=ingest), "POST", "/v1/decisions:log")
        check(c.post("/v1/decisions:log", json={}, headers=ingest), "POST", "/v1/decisions:log")
        too_many = {"records": [{}] * 1001}
        check(
            c.post("/v1/decisions:log", json=too_many, headers=ingest), "POST", "/v1/decisions:log"
        )

        arrived = [o for t, o in data.outcomes if t <= datetime.now(UTC)][:50]
        r = c.post("/v1/outcomes", json={"outcomes": arrived}, headers=ingest)
        assert r.json()["matched"] > 0
        check(r, "POST", "/v1/outcomes")
        early = {
            "outcomes": [
                *arrived[:1],
                {"schema": "outcome/1"},
                arrived[0] | {"external_ref": "later", "outcome_id": str(uuid.uuid4())},
            ]
        }
        check(c.post("/v1/outcomes", json=early, headers=ingest), "POST", "/v1/outcomes")
        check(c.post("/v1/outcomes", json={"outcomes": []}, headers=ingest), "POST", "/v1/outcomes")

        report = {
            "decision_id": data.records[0]["decision_id"],
            "action_taken": "ACCEPT",
            "action_source": "policy",
        }
        actions = {"actions": [report, report | {"decision_id": str(uuid.uuid4())}]}
        check(
            c.post("/v1/decisions:actions", json=actions, headers=ingest),
            "POST",
            "/v1/decisions:actions",
        )
        actions = {"actions": [report | {"action_taken": "MAYBE"}]}
        check(
            c.post("/v1/decisions:actions", json=actions, headers=ingest),
            "POST",
            "/v1/decisions:actions",
        )

        template = "/v1/admin/questions/{question}"
        check(c.get("/v1/admin/questions/refund_fraud", headers=admin), "GET", template)
        check(c.get(f"/v1/admin/questions/{data.question_id}", headers=admin), "GET", template)
        check(c.get("/v1/admin/questions/nope", headers=admin), "GET", template)
        rule = {"settings": {"default_rule": {"after_days": 120, "label": False}}}
        check(
            c.patch("/v1/admin/questions/refund_fraud", json=rule, headers=admin), "PATCH", template
        )
        bad_rule = {"settings": {"default_rule": {"after_days": 120, "label": "yes"}}}
        check(
            c.patch("/v1/admin/questions/refund_fraud", json=bad_rule, headers=admin),
            "PATCH",
            template,
        )

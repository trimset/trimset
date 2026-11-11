"""The policy lifecycle (FR-27 to FR-29) and the audit log (FR-54), against a real
Postgres (CI)."""

import json
import uuid
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from trimset_server import keys
from trimset_server.app import create_app
from trimset_server.cli import _admin_command
from trimset_server.config import Settings
from trimset_server.migrate import run_migrations
from trimset_tools.datagen import generate, send

pytestmark = [pytest.mark.anyio, pytest.mark.integration]
EXAMPLE = Path(__file__).parents[2] / "docs" / "03-api" / "examples" / "policy.json"
MODEL = "jev-1.14.0"


async def _setup(dsn: str) -> tuple[dict[str, str], str, list[str]]:
    """A project with refunds, and three draft policies v1-v3 for refund_fraud on MODEL.
    Returns the keys, the admin key's id and the policy ids."""
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        pid = await keys.create_project(conn, "acme")
        _, ingest = await keys.create_key(conn, pid, "ingest", "datagen")
        admin_row, admin = await keys.create_key(conn, pid, "admin", "ops")
        secrets = {"ingest": ingest, "admin": admin}
        now = datetime.now(UTC)
        with TestClient(create_app(Settings(database_url=dsn))) as c:
            send(generate("refund-fraud", seed=1, end=now, days=1, per_day=5), c, ingest, now=now)
        example = json.loads(EXAMPLE.read_text())
        fit_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO fits (fit_id, project_id, question_id, model_served, body, splits) "
            "VALUES ($1, $2, $3, $4, '{}', '')",
            fit_id,
            pid,
            example["question_id"],
            MODEL,
        )
        ids = []
        for version in (1, 2, 3):
            policy_id = uuid.uuid4()
            body = example | {
                "policy_id": str(policy_id),
                "version": version,
                "fit_id": str(fit_id),
            }
            await conn.execute(
                "INSERT INTO policies (policy_id, project_id, question_id, model_served, version, "
                "fit_id, body, fingerprint) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)",
                policy_id,
                pid,
                example["question_id"],
                MODEL,
                version,
                fit_id,
                json.dumps(body),
                body["fingerprint"],
            )
            ids.append(str(policy_id))
    finally:
        await conn.close()
    return secrets, str(admin_row["key_id"]), ids


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.req("FR-27")
@pytest.mark.req("FR-28")
@pytest.mark.req("FR-29")
async def test_approve_activate_and_roll_back(dsn: str, check: Any) -> None:
    secrets, admin_id, (v1, v2, v3) = await _setup(dsn)
    auth = _auth(secrets["admin"])
    with TestClient(create_app(Settings(database_url=dsn))) as c:

        def step(policy: str, action: str, reason: str | None = None, status: int = 200) -> Any:
            body = {"action": action} | ({"reason": reason} if reason else {})
            r = c.post(f"/v1/admin/policies/{policy}/transitions", json=body, headers=auth)
            check(r, "POST", "/v1/admin/policies/{policy_id}/transitions")
            assert r.status_code == status, r.json()
            return r.json()

        def states() -> dict[str, str]:
            r = c.get("/v1/admin/policies", headers=auth)
            return {p["policy"]["policy_id"]: p["state"] for p in r.json()["items"]}

        # FR-28: an ingest key can't move a policy.
        r = c.post(
            f"/v1/admin/policies/{v1}/transitions",
            json={"action": "submit"},
            headers=_auth(secrets["ingest"]),
        )
        assert r.status_code == 403

        # FR-27: only the allowed transitions, and reject needs a reason.
        assert step(v1, "approve", status=409)["type"].endswith("/invalid-transition")
        step(v1, "submit")
        assert step(v1, "reject", status=422)["status"] == 422
        assert step(v1, "reject", reason="   ", status=422)["status"] == 422
        step(v1, "approve")
        view = step(v1, "activate", reason="first policy")
        assert view["state"] == "active"
        assert [(e["from_state"], e["to_state"]) for e in view["events"]] == [
            ("draft", "proposed"),
            ("proposed", "approved"),
            ("approved", "active"),
        ]
        assert {e["key_id"] for e in view["events"]} == {admin_id}
        assert view["events"][-1]["reason"] == "first policy"

        # FR-29: activating v2 retires v1.
        for action in ("submit", "approve", "activate"):
            step(v2, action)
        assert states() == {v1: "retired", v2: "active", v3: "draft"}
        old = c.get(f"/v1/admin/policies/{v1}", headers=auth).json()
        assert old["events"][-1]["reason"] == "replaced by v2"

        # A rejected policy stays rejected.
        step(v3, "submit")
        step(v3, "reject", reason="escalates too much")
        assert step(v3, "submit", status=409)["status"] == 409

        # Rollback: needs a reason, brings back v1, and can't go further back.
        rollback = "/v1/admin/questions/refund_fraud/rollback"
        r = c.post(rollback, json={"model_served": MODEL}, headers=auth)
        check(r, "POST", "/v1/admin/questions/{question}/rollback")
        assert r.status_code == 422
        r = c.post(
            rollback, json={"model_served": MODEL, "reason": "v2 blocks refunds"}, headers=auth
        )
        check(r, "POST", "/v1/admin/questions/{question}/rollback")
        assert r.status_code == 200 and r.json()["policy"]["policy_id"] == v1, r.json()
        assert r.json()["events"][-1] == r.json()["events"][-1] | {
            "from_state": "retired",
            "to_state": "active",
            "reason": "v2 blocks refunds",
        }
        assert states() == {v1: "active", v2: "retired", v3: "rejected"}
        r = c.post(rollback, json={"model_served": MODEL, "reason": "again"}, headers=auth)
        assert r.status_code == 409 and r.json()["type"].endswith("/nothing-to-roll-back-to")

        # Retire needs a reason; with nothing active, there's nothing to roll back.
        step(v1, "retire", status=422)
        step(v1, "retire", reason="model withdrawn")
        r = c.post(rollback, json={"model_served": MODEL, "reason": "x"}, headers=auth)
        assert r.status_code == 409 and r.json()["type"].endswith("/nothing-active")
        assert c.get("/v1/admin/policies", params={"state": "active"}, headers=auth).json() == {
            "items": [],
            "next_cursor": None,
        }

        # FR-54: every transition is in the audit log, newest first.
        r = c.get("/v1/admin/audit-log", headers=auth)
        check(r, "GET", "/v1/admin/audit-log")
        entries = r.json()["items"]
        moves = [(e["action"], e["target"].removeprefix("policy:")) for e in entries]
        assert moves[:3] == [
            ("policy.retire", v1),
            ("policy.rollback", v1),
            ("policy.rollback", v2),
        ]
        rollback_v1 = entries[1]
        assert rollback_v1["before"] == {"state": "retired"}
        assert rollback_v1["after"] == {"state": "active", "reason": "v2 blocks refunds"}
        assert rollback_v1["key_id"] == admin_id
        # v1: 3, v2: 3 and v1's retirement, v3: 2, the rollback: 2, the last retire: 1.
        assert len([m for m in moves if m[0].startswith("policy.")]) == 12


async def test_one_active_policy_per_group_in_the_database(dsn: str) -> None:
    _, _, (v1, v2, _) = await _setup(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "UPDATE policies SET state = 'active' WHERE policy_id = $1", uuid.UUID(v1)
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "UPDATE policies SET state = 'active' WHERE policy_id = $1", uuid.UUID(v2)
            )
    finally:
        await conn.close()


@pytest.mark.req("FR-54")
async def test_keys_and_settings_changes_are_audited(dsn: str, check: Any) -> None:
    secrets, admin_id, _ = await _setup(dsn)
    auth = _auth(secrets["admin"])
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        made = c.post("/v1/admin/keys", json={"scope": "ingest", "label": "billing"}, headers=auth)
        key_id = made.json()["key_id"]
        for _ in range(2):  # revoking twice changes, and logs, once
            assert c.delete(f"/v1/admin/keys/{key_id}", headers=auth).status_code == 204
        patch = {"retention_days": 200}
        assert c.patch("/v1/admin/project", json=patch, headers=auth).status_code == 200
        assert c.patch("/v1/admin/project", json=patch, headers=auth).status_code == 200
        horizon = {"settings": {"label_horizon_days": 120}}
        r = c.patch("/v1/admin/questions/refund_fraud", json=horizon, headers=auth)
        assert r.status_code == 200, r.json()

        r = c.get("/v1/admin/audit-log", headers=auth)
        check(r, "GET", "/v1/admin/audit-log")
        entries = r.json()["items"]
        assert [e["action"] for e in entries] == [
            "question.update",
            "project.settings",
            "key.revoke",
            "key.create",
        ]
        question, project, revoke, create = entries
        assert {e["key_id"] for e in entries} == {admin_id}
        assert question["after"]["settings"]["label_horizon_days"] == 120
        assert "label_horizon_days" not in question["before"]["settings"]
        assert question["before"]["settings"].items() <= question["after"]["settings"].items()
        assert project["after"]["retention_days"] == 200
        assert project["before"].get("retention_days") is None
        assert revoke["target"] == create["target"] == f"key:{key_id}"
        assert revoke["before"]["revoked"] is False and revoke["after"]["revoked"] is True
        assert create["after"] == {
            "scope": "ingest",
            "label": "billing",
            "prefix": made.json()["prefix"],
        }
        assert "secret" not in json.dumps(entries)

        r = c.get("/v1/admin/audit-log", params={"limit": 1}, headers=auth)
        assert [e["action"] for e in r.json()["items"]] == ["question.update"]
        r = c.get("/v1/admin/audit-log", params={"from": "not a time"}, headers=auth)
        assert r.status_code == 422


async def test_server_commands_are_audited_without_a_key(dsn: str) -> None:
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        await keys.create_project(conn, "acme")
    finally:
        await conn.close()
    create = Namespace(command="create-key", project="acme", scope="ingest", label="server")
    key_id = (await _admin_command(create, dsn))["key_id"]
    for _ in range(2):
        await _admin_command(Namespace(command="revoke-key", key_id=uuid.UUID(key_id)), dsn)
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT key_id, action, target FROM audit_log ORDER BY id")
    finally:
        await conn.close()
    assert [tuple(r) for r in rows] == [
        (None, "key.create", f"key:{key_id}"),
        (None, "key.revoke", f"key:{key_id}"),
    ]

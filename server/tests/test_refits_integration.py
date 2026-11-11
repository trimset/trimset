"""Scheduled refits only propose (T3.3, FR-30), against a real Postgres (CI)."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from trimset_server import fitting, jobs, keys, refits
from trimset_server.app import create_app
from trimset_server.config import Settings
from trimset_server.migrate import run_migrations
from trimset_tools.datagen import generate, send

pytestmark = [pytest.mark.anyio, pytest.mark.integration]
MODEL = {"model_served": "jev-1.14.0"}


async def _db(dsn: str, fn: Any) -> Any:
    conn = await asyncpg.connect(dsn)
    try:
        return await fn(conn)
    finally:
        await conn.close()


async def _last_refit(conn: Any) -> Any:
    row = await conn.fetchrow(
        "SELECT status, result FROM jobs WHERE kind = 'refit' ORDER BY job_id DESC LIMIT 1"
    )
    return row["status"], json.loads(row["result"]) if row["result"] else None


@pytest.mark.req("FR-30")
async def test_a_refit_proposes_replaces_its_last_proposal_and_never_activates(
    dsn: str, check: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_migrations(dsn)

    async def made(conn: Any) -> tuple[str, str]:
        pid = await keys.create_project(conn, "demo")
        ingest = (await keys.create_key(conn, pid, "ingest", "datagen"))[1]
        return ingest, (await keys.create_key(conn, pid, "admin", "ops"))[1]

    ingest, admin = await _db(dsn, made)
    auth = {"Authorization": f"Bearer {admin}"}
    now = datetime.now(UTC)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        send(generate("ticket-triage", seed=2, end=now, days=10, per_day=200), c, ingest, now=now)
        horizon = {"settings": {"label_horizon_days": 3}}
        assert c.patch("/v1/admin/questions/team", json=horizon, headers=auth).status_code == 200
        bad = c.patch("/v1/admin/project", json={"refit_schedule": "0 25 * * *"}, headers=auth)
        assert bad.status_code == 422 and "outside 0-23" in json.dumps(bad.json())

        # v1: fitted, compiled and activated by hand.
        c.post("/v1/admin/questions/team/fits", json=MODEL, headers=auth)
        await _db(dsn, jobs.work)
        target = {"kind": "risk", "metric": "error_budget", "alpha": 0.4}  # see test_fitting
        r = c.post(
            "/v1/admin/questions/team/compile", json=MODEL | {"target": target}, headers=auth
        )
        await _db(dsn, jobs.work)
        done = c.get(r.headers["location"], headers=auth).json()
        assert "policy_id" in (done["result"] or {}), done
        (v1,) = c.get("/v1/admin/policies", headers=auth).json()["items"]
        v1_id = v1["policy"]["policy_id"]
        for action in ("submit", "approve", "activate"):
            r = c.post(
                f"/v1/admin/policies/{v1_id}/transitions", json={"action": action}, headers=auth
            )
            assert r.status_code == 200, r.json()

        # The schedule (daily at 03:00 UTC) queues one job per due minute, once.
        tick = now.replace(hour=3, minute=0, second=30)
        assert await _db(dsn, lambda conn: refits.schedule(conn, tick)) == 1
        assert await _db(dsn, lambda conn: refits.schedule(conn, tick)) == 0
        assert await _db(dsn, lambda conn: refits.schedule(conn, tick + timedelta(hours=5))) == 0

        # A refit that finds a different policy proposes it, and activates nothing.
        monkeypatch.setattr(fitting, "same_policy", lambda *_: False)
        await _db(dsn, jobs.work)
        status, result = await _db(dsn, _last_refit)
        assert status == "succeeded", result
        (group,) = result["groups"]
        assert group["question_id"] == v1["policy"]["question_id"] and group["proposal_ready"]
        v2_id = group["policy_id"]
        v2 = c.get(f"/v1/admin/policies/{v2_id}", headers=auth).json()
        assert v2["state"] == "proposed"
        (created,) = v2["events"]
        assert created == created | {
            "from_state": None,
            "to_state": "proposed",
            "key_id": None,
            "reason": "scheduled refit",
        }
        assert v2["policy"]["target"] == v1["policy"]["target"]
        active = c.get("/v1/admin/policies", params={"state": "active"}, headers=auth).json()
        assert [p["policy"]["policy_id"] for p in active["items"]] == [v1_id]

        # Its diff against the active policy shows who supplied the tuning labels.
        diff = c.get(f"/v1/admin/policies/{v2_id}/diff", headers=auth)
        check(diff, "GET", "/v1/admin/policies/{policy_id}/diff")
        sources = diff.json()["label_sources"]
        assert sources and abs(sum(s["share"] for s in sources) - 1) < 0.01
        assert {s["source"] for s in sources} <= {"human_review", "downstream", "rule"}

        # The next day's refit replaces the proposal still waiting.
        assert await _db(dsn, lambda conn: refits.schedule(conn, tick + timedelta(days=1))) == 1
        await _db(dsn, jobs.work)
        _, result = await _db(dsn, _last_refit)
        v3_version = result["groups"][0]["version"]
        v2 = c.get(f"/v1/admin/policies/{v2_id}", headers=auth).json()
        assert v2["state"] == "rejected"
        assert v2["events"][-1]["reason"] == f"superseded by v{v3_version}"

        # When the refit finds the same policy, it proposes nothing.
        monkeypatch.setattr(fitting, "same_policy", lambda *_: True)
        assert await _db(dsn, lambda conn: refits.schedule(conn, tick + timedelta(days=2))) == 1
        await _db(dsn, jobs.work)
        _, result = await _db(dsn, _last_refit)
        assert result["groups"][0]["unchanged"] == v1_id
        listed = c.get("/v1/admin/policies", headers=auth).json()["items"]
        assert len(listed) == 3

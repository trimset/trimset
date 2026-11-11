"""Fit and compile jobs, the simulator and coverage, against a real Postgres (CI)."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from trimset.hashing import fingerprint
from trimset_server import jobs, keys
from trimset_server.app import create_app
from trimset_server.config import Settings
from trimset_server.migrate import run_migrations
from trimset_tools.datagen import generate, send

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _setup(
    dsn: str, scenario: str = "ticket-triage", days: float = 10, per_day: int = 200
) -> tuple[Any, str]:
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        pid = await keys.create_project(conn, "demo")
        ingest = (await keys.create_key(conn, pid, "ingest", "datagen"))[1]
        admin = (await keys.create_key(conn, pid, "admin", "ops"))[1]
    finally:
        await conn.close()
    if days:
        now = datetime.now(UTC)
        with TestClient(create_app(Settings(database_url=dsn))) as c:
            send(
                generate(scenario, seed=2, end=now, days=days, per_day=per_day), c, ingest, now=now
            )
    return pid, admin


async def _work(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        return await jobs.work(conn)
    finally:
        await conn.close()


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.req("FR-16")
@pytest.mark.req("FR-22")
@pytest.mark.req("FR-26")
async def test_fit_then_compile_then_simulate(dsn: str, check: Any, conforms: Any) -> None:
    _, admin = await _setup(dsn)
    auth = _auth(admin)
    model = {"model_served": "jev-1.14.0"}
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        # Tickets close within 72 hours: younger ones haven't had their chance to be
        # labeled, and counting them as wrong would make any target impossible.
        horizon = {"settings": {"label_horizon_days": 3}}
        assert c.patch("/v1/admin/questions/team", json=horizon, headers=auth).status_code == 200
        r = c.post("/v1/admin/questions/team/fits", json=model, headers=auth)
        check(r, "POST", "/v1/admin/questions/{question}/fits")
        assert (
            r.status_code == 202 and r.headers["location"] == f"/v1/admin/jobs/{r.json()['job_id']}"
        )
        assert r.json()["status"] == "queued"
        assert await _work(dsn) == 1

        job = c.get(r.headers["location"], headers=auth)
        check(job, "GET", "/v1/admin/jobs/{job_id}")
        assert job.json()["status"] == "succeeded", job.json()
        fit_id = job.json()["result"]["fit_id"]
        fit = c.get(f"/v1/admin/fits/{fit_id}", headers=auth)
        check(fit, "GET", "/v1/admin/fits/{fit_id}")
        assert fit.json()["type"] == "choice"
        listed = c.get("/v1/admin/questions/team/fits", params=model, headers=auth)
        check(listed, "GET", "/v1/admin/questions/{question}/fits")
        assert [f["fit_id"] for f in listed.json()["items"]] == [fit_id]

        # 5% of tickets never get a webhook, and an unlabeled item counts as wrong.
        target = {"kind": "risk", "metric": "error_budget", "alpha": 0.25}
        for _ in range(2):
            r = c.post(
                "/v1/admin/questions/team/compile",
                json=model | {"fit_id": fit_id, "target": target},
                headers=auth,
            )
            check(r, "POST", "/v1/admin/questions/{question}/compile")
            await _work(dsn)
        done = c.get(r.headers["location"], headers=auth).json()
        assert done["status"] == "succeeded" and "policy_id" in done["result"], done
        assert done["result"]["version"] == 2

        sim = c.post("/v1/admin/questions/team/simulate", json=model, headers=auth)
        check(sim, "POST", "/v1/admin/questions/{question}/simulate")
        assert sim.json()["fit_id"] == fit_id and len(sim.json()["rows"]) > 1
        one = {"id": "select", "action": "SELECT", "scalar": "top", "op": "ge", "threshold": 0.9}
        sim = c.post(
            "/v1/admin/questions/team/simulate",
            json=model | {"fit_id": fit_id, "candidates": [[one]]},  # a uuid as JSON sends it
            headers=auth,
        )
        check(sim, "POST", "/v1/admin/questions/{question}/simulate")
        assert sim.json()["fit_id"] == fit_id
        (row,) = sim.json()["rows"]
        assert row["rules"] == [one]

        cov = c.get("/v1/admin/questions/team/coverage", headers=auth)
        check(cov, "GET", "/v1/admin/questions/{question}/coverage")
        paths = {p["action"]: p for p in cov.json()["paths"]}
        assert {"SELECT", "ESCALATE"} <= set(paths)
        assert all(p["labeled"] <= p["decisions"] for p in paths.values())

        listed = c.get(
            "/v1/admin/policies",
            params={"question": "team", "model_served": "jev-1.14.0"},
            headers=auth,
        )
        check(listed, "GET", "/v1/admin/policies")
        newest, oldest = listed.json()["items"]
        assert (newest["policy"]["version"], newest["state"]) == (2, "draft")
        assert newest["guarantee_status"] == "certified"
        (created,) = newest["events"]  # FR-27: compiled as a draft, by the key that asked
        assert (created["from_state"], created["to_state"]) == (None, "draft")
        assert created["key_id"] is not None and created["reason"] is None
        second, first = newest["policy"]["policy_id"], oldest["policy"]["policy_id"]
        check(
            c.get(f"/v1/admin/policies/{second}", headers=auth),
            "GET",
            "/v1/admin/policies/{policy_id}",
        )
        diff = c.get(f"/v1/admin/policies/{second}/diff", params={"against": first}, headers=auth)
        check(diff, "GET", "/v1/admin/policies/{policy_id}/diff")
        assert diff.json()["against"] == first and [r["id"] for r in diff.json()["rules"]] == [
            "select"
        ]
        alone = c.get(f"/v1/admin/policies/{second}/diff", headers=auth).json()
        assert alone["against"] is None and alone["metrics"]["before"] is None
        assert c.get(f"/v1/admin/policies/{uuid.uuid4()}", headers=auth).status_code == 404
        assert (
            c.get("/v1/admin/policies", params={"state": "nope"}, headers=auth).status_code == 422
        )

        latest = c.get("/v1/admin/decisions", params={"question": "team", "limit": 5}, headers=auth)
        check(latest, "GET", "/v1/admin/decisions")
        items = latest.json()["items"]
        assert len(items) == 5 and [d["ts"] for d in items] == sorted(
            (d["ts"] for d in items), reverse=True
        )
        newer = c.get("/v1/admin/decisions", params={"from": items[1]["ts"]}, headers=auth).json()
        assert [d["decision_id"] for d in newer["items"]] == [items[0]["decision_id"]]
        selected = c.get(
            "/v1/admin/decisions", params={"action": "SELECT", "limit": 3}, headers=auth
        )
        assert all(d["action_taken"].startswith("SELECT:") for d in selected.json()["items"])
        pending = c.get("/v1/admin/decisions", params={"outcome": "pending"}, headers=auth)
        check(pending, "GET", "/v1/admin/decisions")
        assert (
            c.get("/v1/admin/decisions", params={"outcome": "right"}, headers=auth).status_code
            == 422
        )

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT body, fingerprint, state, version FROM policies ORDER BY version"
        )
    finally:
        await conn.close()
    assert [(r["state"], r["version"]) for r in rows] == [("draft", 1), ("draft", 2)]
    policy = json.loads(rows[1]["body"])
    conforms(policy, "policy")
    assert policy["fingerprint"] == rows[1]["fingerprint"] == fingerprint(policy)
    assert policy["guarantee"]["status"] == "certified"
    assert policy["fit_id"] == fit_id and policy["question_name"] == "team"


@pytest.mark.req("FR-19")
async def test_a_fit_without_enough_labels_says_so(dsn: str) -> None:
    _, admin = await _setup(dsn, days=1, per_day=50)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        r = c.post(
            "/v1/admin/questions/team/fits",
            json={"model_served": "jev-1.14.0"},
            headers=_auth(admin),
        )
        await _work(dsn)
        job = c.get(r.headers["location"], headers=_auth(admin)).json()
    assert job["status"] == "succeeded"
    assert job["result"]["not_enough_labels"]["needed"] > 0


async def test_compiling_without_a_fit_fails_with_a_reason(dsn: str) -> None:
    _, admin = await _setup(dsn, days=1, per_day=20)
    target = {"kind": "risk", "metric": "error_budget", "alpha": 0.1}
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        r = c.post(
            "/v1/admin/questions/team/compile",
            json={"model_served": "jev-1.14.0", "target": target},
            headers=_auth(admin),
        )
        await _work(dsn)
        job = c.get(r.headers["location"], headers=_auth(admin)).json()
        bad = c.post(
            "/v1/admin/questions/team/compile",
            json={"model_served": "jev-1.14.0", "target": {"kind": "risk"}},
            headers=_auth(admin),
        )
        assert bad.status_code == 422
        missing = c.post(
            "/v1/admin/questions/nope/fits", json={"model_served": "m"}, headers=_auth(admin)
        )
        assert missing.status_code == 404
        assert c.get("/v1/admin/jobs/999999", headers=_auth(admin)).status_code == 404
        assert c.get(f"/v1/admin/fits/{uuid.uuid4()}", headers=_auth(admin)).status_code == 404
    assert (job["status"], job["error"]) == (
        "failed",
        "No fit for this question and model: run a fit first",
    )


async def test_a_job_that_keeps_failing_is_retried_then_given_up(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, _ = await _setup(dsn, days=0)

    async def boom(*_: Any) -> dict[str, Any]:
        raise RuntimeError("database fell over")

    monkeypatch.setitem(jobs.HANDLERS, "fit", boom)
    conn = await asyncpg.connect(dsn)
    try:
        job_id = await conn.fetchval(
            "INSERT INTO jobs (project_id, kind, payload) VALUES ($1, 'fit', '{}') "
            "RETURNING job_id",
            pid,
        )
        for attempt in (1, 2):
            assert await jobs.run_one(conn)
            row = await conn.fetchrow("SELECT status, attempts, run_at > now() AS later FROM jobs")
            assert (row["status"], row["attempts"], row["later"]) == ("queued", attempt, True)
            await conn.execute("UPDATE jobs SET run_at = now()")  # skip the backoff
        assert await jobs.run_one(conn)
        row = await conn.fetchrow("SELECT status, error FROM jobs WHERE job_id = $1", job_id)
        assert (row["status"], row["error"]) == ("failed", "failed 3 times (RuntimeError)")
        assert not await jobs.run_one(conn)
    finally:
        await conn.close()


@pytest.mark.req("FR-20")
async def test_action_settings_must_fit_the_question(dsn: str) -> None:
    _, admin = await _setup(dsn, days=1, per_day=10)
    with TestClient(create_app(Settings(database_url=dsn))) as c:

        def patch(settings: dict[str, Any]) -> int:
            body = {"settings": settings}
            return c.patch("/v1/admin/questions/team", json=body, headers=_auth(admin)).status_code

        assert patch({"actions": {"block_options": ["tech"]}, "audit_fraction": 0.05}) == 200
        assert patch({"actions": {"block_options": ["sales"]}}) == 422
        assert patch({"actions": {"high_means": "accept"}}) == 422  # noul only
        assert patch({"actions": {"block_from_level": 2}}) == 422  # score only
        assert patch({"audit_fraction": 0.9}) == 422

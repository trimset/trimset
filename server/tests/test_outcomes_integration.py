"""Outcomes, held joins, default rules, question settings and action reports against a
real Postgres (CI)."""

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from trimset.wire import parse_exchange, to_records, uuid7
from trimset_server import actions, keys, outcomes
from trimset_server.app import create_app
from trimset_server.config import Settings
from trimset_server.migrate import run_migrations
from trimset_server.worker import run_worker

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

REQUEST = {
    "model": "jev-latest",
    "state": {"refund_id": "rf-1001"},
    "questions": {
        "refund_fraud": {"type": "noul", "instructions": "Is this refund request fraudulent?"},
        "team": {
            "type": "choice",
            "instructions": "Which team should handle this ticket?",
            "criteria": {"billing": None, "shipping": None},
        },
    },
}
RESPONSE = {
    "model": "jev-1.14.0",
    "answers": {
        "refund_fraud": {"type": "noul", "noul": 0.83},
        "team": {"type": "choice", "choice": "billing", "confidence": 0.7,
                 "probabilities": {"billing": 0.7, "shipping": 0.3}},
    },
}  # fmt: skip
NOW = datetime.now(UTC).isoformat()


async def _setup(dsn: str) -> tuple[str, str]:
    """A project with an ingest key and an admin key."""
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        pid = await keys.create_project(conn, "acme")
        _, ingest = await keys.create_key(conn, pid, "ingest", "app")
        _, admin = await keys.create_key(conn, pid, "admin", "ops")
        return ingest, admin
    finally:
        await conn.close()


def _log(
    c: TestClient,
    key: str,
    *,
    ref: str | None = None,
    ts: datetime | None = None,
    ids: list[str] | None = None,
    request: dict[str, Any] = REQUEST,
) -> dict[str, str]:
    """Logs one exchange; returns the decision ids by question name."""
    ex = parse_exchange(request, RESPONSE, "systemone")
    records = to_records(
        ex, ts=ts or datetime.now(UTC), decision_ids=ids, external_ref=ref, request_id=uuid7()
    )
    body = {"questions": {d.question_id: d.definition for d in ex.decisions}, "records": records}
    r = c.post("/v1/decisions:log", json=body, headers=_auth(key))
    assert r.json()["stored"] == len(records), r.json()
    return {rec["question_name"]: rec["decision_id"] for rec in records}


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _outcome(label: Any, **ref: Any) -> dict[str, Any]:
    return {"schema": "outcome/1", "label": label, "source": "downstream", "observed_at": NOW} | ref


def _send(c: TestClient, key: str, *items: dict[str, Any]) -> Any:
    return c.post("/v1/outcomes", json={"outcomes": list(items)}, headers=_auth(key))


async def _rows(dsn: str, sql: str, *args: Any) -> list[Any]:
    conn = await asyncpg.connect(dsn)
    try:
        return list(await conn.fetch(sql, *args))
    finally:
        await conn.close()


async def _call(dsn: str, job: Any) -> Any:
    conn = await asyncpg.connect(dsn)
    try:
        return await job(conn)
    finally:
        await conn.close()


async def _current(dsn: str) -> dict[str, Any]:
    rows = await _rows(dsn, "SELECT decision_id, label, source FROM outcomes")
    return {str(r["decision_id"]): (json.loads(r["label"]), r["source"]) for r in rows}


@pytest.mark.req("FR-10")
async def test_outcomes_match_by_decision_id_or_by_external_ref(dsn: str) -> None:
    ingest, _ = await _setup(dsn)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        ids = _log(c, ingest, ref="rf-1")
        r = _send(
            c,
            ingest,
            _outcome(True, decision_id=ids["refund_fraud"]),
            _outcome("billing", external_ref="rf-1", question="team"),
        )
    assert r.status_code == 200
    assert r.json() == {
        "matched": 2,
        "held": 0,
        "rejected": 0,
        "results": [
            {"index": 0, "status": "matched", "decision_ids": [ids["refund_fraud"]]},
            {"index": 1, "status": "matched", "decision_ids": [ids["team"]]},
        ],
    }
    assert await _current(dsn) == {
        ids["refund_fraud"]: (True, "downstream"),
        ids["team"]: ("billing", "downstream"),
    }


@pytest.mark.req("FR-10")
async def test_a_name_covers_the_wordings_it_had_before(dsn: str) -> None:
    ingest, _ = await _setup(dsn)
    reworded = json.loads(json.dumps(REQUEST))
    reworded["questions"]["refund_fraud"]["instructions"] = "Is this refund fraudulent?"
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        old = _log(c, ingest, ref="rf-1")
        _log(c, ingest, ref="rf-2", request=reworded)  # the name moves to this wording
        r = _send(c, ingest, _outcome(False, external_ref="rf-1", question="refund_fraud"))
    assert r.json()["results"][0]["decision_ids"] == [old["refund_fraud"]]


@pytest.mark.req("FR-11")
async def test_an_early_outcome_is_held_until_its_decision_arrives(dsn: str) -> None:
    ingest, _ = await _setup(dsn)
    early_id = uuid7()
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        r = _send(
            c,
            ingest,
            _outcome(True, external_ref="rf-9", question="refund_fraud"),
            _outcome("shipping", decision_id=early_id),
            _outcome(True, external_ref="rf-never", question="refund_fraud"),
        )
        assert (r.status_code, r.json()["held"]) == (200, 3)
        assert await _call(dsn, outcomes.join_held) == (0, 0)  # nothing to join yet

        ids = _log(c, ingest, ref="rf-9", ids=[uuid7(), early_id])
    await _rows(
        dsn,
        "UPDATE unmatched_outcomes SET expires_at = now() - interval '1 second' "
        "WHERE external_ref = 'rf-never'",
    )
    assert await _call(dsn, outcomes.join_held) == (2, 1)
    assert await _current(dsn) == {
        ids["refund_fraud"]: (True, "downstream"),
        early_id: ("shipping", "downstream"),
    }
    assert await _rows(dsn, "SELECT id FROM unmatched_outcomes") == []


@pytest.mark.req("FR-12")
async def test_the_last_label_received_wins_and_history_keeps_them_all(dsn: str) -> None:
    ingest, _ = await _setup(dsn)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        ids = _log(c, ingest, ref="rf-1")
        fraud = ids["refund_fraud"]
        _send(c, ingest, _outcome(True, decision_id=fraud))
        _send(c, ingest, _outcome(False, decision_id=fraud))  # a correction
    assert (await _current(dsn))[fraud] == (False, "downstream")
    history = await _rows(
        dsn,
        "SELECT label FROM outcome_history WHERE decision_id = $1 ORDER BY id",
        uuid.UUID(fraud),
    )
    assert [json.loads(r["label"]) for r in history] == [True, False]


@pytest.mark.req("FR-12")
async def test_a_late_held_label_never_overrides_a_newer_one(dsn: str) -> None:
    ingest, _ = await _setup(dsn)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        _send(c, ingest, _outcome("billing", external_ref="rf-3", question="team"))  # held
        ids = _log(c, ingest, ref="rf-3")
        _send(c, ingest, _outcome("shipping", decision_id=ids["team"]))  # newer
    assert await _call(dsn, outcomes.join_held) == (1, 0)
    assert (await _current(dsn))[ids["team"]] == ("shipping", "downstream")
    history = await _rows(
        dsn, "SELECT label FROM outcome_history WHERE decision_id = $1", uuid.UUID(ids["team"])
    )
    assert sorted(json.loads(r["label"]) for r in history) == ["billing", "shipping"]


@pytest.mark.req("FR-10")
async def test_an_outcome_id_counts_once(dsn: str) -> None:
    ingest, _ = await _setup(dsn)
    oid = uuid7()
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        ids = _log(c, ingest)
        same = _outcome(True, decision_id=ids["refund_fraud"], outcome_id=oid)
        first = _send(c, ingest, same, same)
        assert [x["status"] for x in first.json()["results"]] == ["matched", "duplicate"]
        again = _send(c, ingest, same)
        assert again.json()["results"][0]["status"] == "duplicate"
        held = _outcome(True, decision_id=uuid7(), outcome_id=uuid7())
        assert _send(c, ingest, held).json()["held"] == 1
        assert _send(c, ingest, held).json()["results"][0]["status"] == "duplicate"
    assert len(await _rows(dsn, "SELECT id FROM outcome_history")) == 1


@pytest.mark.req("FR-10")
async def test_labels_that_dont_fit_are_rejected_and_the_rest_kept(dsn: str) -> None:
    ingest, _ = await _setup(dsn)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        ids = _log(c, ingest)
        r = _send(
            c,
            ingest,
            _outcome(True, decision_id=ids["refund_fraud"]),
            _outcome("true", decision_id=ids["refund_fraud"]),  # a string, not a boolean
            _outcome("sales", decision_id=ids["team"]),  # not an option
            _outcome(0.5, decision_id=ids["team"]),  # not a label at all
            _outcome(True, external_ref="rf-1"),  # no question
            _outcome("ann@example.com\x00", decision_id=ids["team"]),
        )
    assert r.status_code == 207
    body = r.json()
    assert (body["matched"], body["rejected"]) == (1, 5)
    assert [x["status"] for x in body["results"]] == ["matched"] + ["rejected"] * 5
    assert body["results"][1]["message"] == "the label doesn't fit the question"
    assert "ann@example.com" not in json.dumps(body)


@pytest.mark.parametrize(
    "body", [{}, {"outcomes": []}, {"outcomes": {}}, {"outcomes": [], "extra": 1}, [1]]
)
async def test_a_malformed_outcome_batch_is_refused(dsn: str, body: Any) -> None:
    ingest, _ = await _setup(dsn)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        r = c.post("/v1/outcomes", json=body, headers=_auth(ingest))
    assert r.status_code == 400


@pytest.mark.req("FR-13")
async def test_a_default_rule_labels_matured_decisions_once(dsn: str) -> None:
    ingest, admin = await _setup(dsn)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        old = _log(c, ingest, ts=datetime.now(UTC) - timedelta(days=2))
        labelled = _log(c, ingest, ts=datetime.now(UTC) - timedelta(days=2))
        fresh = _log(c, ingest)
        _send(c, ingest, _outcome(True, decision_id=labelled["refund_fraud"]))

        bad = {"settings": {"default_rule": {"after_days": 1, "label": "billing"}}}
        r = c.patch("/v1/admin/questions/refund_fraud", json=bad, headers=_auth(admin))
        assert r.status_code == 422
        rule = {"settings": {"default_rule": {"after_days": 1, "label": False}}}
        r = c.patch("/v1/admin/questions/refund_fraud", json=rule, headers=_auth(admin))
        assert r.status_code == 200
        assert r.json()["settings"] == rule["settings"]

    assert await _call(dsn, outcomes.apply_default_rules) == 1
    assert await _call(dsn, outcomes.apply_default_rules) == 0
    current = await _current(dsn)
    assert current[old["refund_fraud"]] == (False, "rule")
    assert current[labelled["refund_fraud"]] == (True, "downstream")
    assert fresh["refund_fraud"] not in current
    history = await _rows(dsn, "SELECT key_id FROM outcome_history WHERE source = 'rule'")
    assert [r["key_id"] for r in history] == [None]


@pytest.mark.req("FR-03")
async def test_a_question_can_be_read_renamed_and_configured_by_admin_only(dsn: str) -> None:
    ingest, admin = await _setup(dsn)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        _log(c, ingest)
        r = c.get("/v1/admin/questions/refund_fraud", headers=_auth(admin))
        assert r.status_code == 200
        view = r.json()
        assert (view["name"], view["type"], view["settings"]) == ("refund_fraud", "noul", {})
        assert [m["model_served"] for m in view["models"]] == ["jev-1.14.0"]
        by_id = c.get(f"/v1/admin/questions/{view['question_id']}", headers=_auth(admin))
        assert by_id.json()["name"] == "refund_fraud"

        assert c.get("/v1/admin/questions/refund_fraud", headers=_auth(ingest)).status_code == 403
        r = c.patch("/v1/admin/questions/refund_fraud", json={"name": "team"}, headers=_auth(admin))
        assert (r.status_code, r.json()["type"]) == (409, "https://trimset.dev/problems/name-taken")
        r = c.patch(
            "/v1/admin/questions/refund_fraud",
            json={"settings": {"threshold": 0.5}},
            headers=_auth(admin),
        )
        assert r.status_code == 422
        r = c.patch(
            "/v1/admin/questions/refund_fraud", json={"name": "fraud"}, headers=_auth(admin)
        )
        assert r.json()["name"] == "fraud"
        assert c.get("/v1/admin/questions/refund_fraud", headers=_auth(admin)).status_code == 404
        assert c.get("/v1/admin/questions/fraud", headers=_auth(admin)).status_code == 200


@pytest.mark.req("FR-33")
async def test_action_reports_are_applied_or_held_for_their_decision(dsn: str) -> None:
    ingest, _ = await _setup(dsn)
    later = uuid7()
    fingerprint = "sha256:" + "a" * 64
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        ids = _log(c, ingest)
        reports = [
            {
                "decision_id": ids["refund_fraud"],
                "action_taken": "BLOCK",
                "action_source": "policy",
                "policy_version": 3,
                "policy_fingerprint": fingerprint,
            },
            {
                "decision_id": later,
                "action_taken": "ESCALATE",
                "action_source": "default",
                "reason": "no_policy",
            },
            {"decision_id": ids["team"], "action_taken": "MAYBE", "action_source": "policy"},
        ]
        r = c.post("/v1/decisions:actions", json={"actions": reports}, headers=_auth(ingest))
        assert r.status_code == 207
        assert {k: v for k, v in r.json().items() if k != "errors"} == {
            "stored": 1,
            "duplicates": 0,
            "rejected": 1,
            "held": 1,
        }
        assert r.json()["errors"][0]["pointer"] == "/actions/2/action_taken"
        assert await _call(dsn, actions.apply_held) == (0, 0)
        _log(c, ingest, ids=[later, uuid7()])

    assert await _call(dsn, actions.apply_held) == (1, 0)
    rows = await _rows(
        dsn,
        "SELECT decision_id::text AS id, action_taken, action_source, action_reason, "
        "policy_version, policy_fingerprint FROM decisions WHERE action_taken IS NOT NULL",
    )
    assert {r["id"]: tuple(r)[1:] for r in rows} == {
        ids["refund_fraud"]: ("BLOCK", "policy", None, 3, fingerprint),
        later: ("ESCALATE", "default", "no_policy", None, None),
    }


async def test_held_action_reports_expire(dsn: str) -> None:
    ingest, _ = await _setup(dsn)
    report = {"decision_id": uuid7(), "action_taken": "ACCEPT", "action_source": "default"}
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        r = c.post("/v1/decisions:actions", json={"actions": [report]}, headers=_auth(ingest))
        assert r.json()["held"] == 1
    await _rows(dsn, "UPDATE pending_actions SET expires_at = now() - interval '1 second'")
    assert await _call(dsn, actions.apply_held) == (0, 1)


async def test_the_worker_joins_held_outcomes(dsn: str) -> None:
    ingest, _ = await _setup(dsn)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        _send(c, ingest, _outcome(True, external_ref="rf-5", question="refund_fraud"))
        ids = _log(c, ingest, ref="rf-5")

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(Settings(database_url=dsn, join_interval_s=0.05), stop))
    try:
        for _ in range(100):
            if ids["refund_fraud"] in await _current(dsn):
                break
            await asyncio.sleep(0.05)
        assert ids["refund_fraud"] in await _current(dsn)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.req("FR-11")
async def test_held_outcomes_that_never_match_cant_keep_the_rest_waiting(dsn: str) -> None:
    ingest, _ = await _setup(dsn)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        _log(c, ingest, ref="rf-1")
        _send(
            c,
            ingest,
            _outcome(True, external_ref="rf-never", question="refund_fraud"),  # no decision
            _outcome(True, external_ref="rf-1", question="nope"),  # a decision, not this question
            _outcome(True, external_ref="rf-1", question="nope2"),
            _outcome(True, external_ref="rf-2", question="refund_fraud"),
        )
        ids = _log(c, ingest, ref="rf-2")
    join = outcomes.join_held
    assert await _call(dsn, lambda conn: join(conn, batch=2)) == (0, 0)  # tries the two nopes
    assert await _call(dsn, lambda conn: join(conn, batch=2)) == (1, 0)  # then the rest
    assert ids["refund_fraud"] in await _current(dsn)
    assert len(await _rows(dsn, "SELECT id FROM unmatched_outcomes")) == 3


@pytest.mark.req("FR-33")
async def test_held_action_reports_that_never_match_cant_keep_the_rest_waiting(dsn: str) -> None:
    ingest, _ = await _setup(dsn)
    later = uuid7()
    reports = [
        {"decision_id": d, "action_taken": "ACCEPT", "action_source": "default"}
        for d in (uuid7(), uuid7(), later)
    ]
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        r = c.post("/v1/decisions:actions", json={"actions": reports}, headers=_auth(ingest))
        assert r.json()["held"] == 3
        _log(c, ingest, ids=[later, uuid7()])
    assert await _call(dsn, lambda conn: actions.apply_held(conn, batch=1)) == (1, 0)


async def test_a_project_can_hold_only_so_many(dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    ingest, _ = await _setup(dsn)
    monkeypatch.setattr(outcomes, "MAX_HELD", 2)
    monkeypatch.setattr(actions, "MAX_HELD", 1)
    report = {"action_taken": "ACCEPT", "action_source": "default"}
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        r = _send(c, ingest, *(_outcome(True, decision_id=uuid7()) for _ in range(3)))
        assert r.status_code == 207
        assert [x["status"] for x in r.json()["results"]] == ["held", "held", "rejected"]
        assert (
            r.json()["results"][2]["message"]
            == "2 outcomes are already waiting for their decisions"
        )

        r = c.post(
            "/v1/decisions:actions",
            json={"actions": [report | {"decision_id": uuid7()} for _ in range(2)]},
            headers=_auth(ingest),
        )
        assert r.status_code == 207
        assert (r.json()["held"], r.json()["rejected"]) == (1, 1)
        assert r.json()["errors"][0]["index"] == 1


async def test_requests_at_the_same_time_cant_overshoot_the_limit(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, _ = await _setup(dsn)
    monkeypatch.setattr(outcomes, "MAX_HELD", 5)
    monkeypatch.setattr(actions, "MAX_HELD", 5)
    report = {"action_taken": "ACCEPT", "action_source": "default"}
    with TestClient(create_app(Settings(database_url=dsn))) as c, ThreadPoolExecutor(10) as pool:

        def outcome(_: int) -> Any:
            return _send(c, ingest, _outcome(True, decision_id=uuid7()))

        def action(_: int) -> Any:
            body = {"actions": [report | {"decision_id": uuid7()}]}
            return c.post("/v1/decisions:actions", json=body, headers=_auth(ingest))

        assert sum(r.json()["held"] for r in pool.map(outcome, range(12))) == 5
        assert sum(r.json()["held"] for r in pool.map(action, range(12))) == 5
    assert len(await _rows(dsn, "SELECT id FROM unmatched_outcomes")) == 5
    assert len(await _rows(dsn, "SELECT id FROM pending_actions")) == 5

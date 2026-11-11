"""POST /v1/decisions:log against a real Postgres (CI)."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from trimset.wire import parse_exchange, to_records, uuid7
from trimset_server import keys
from trimset_server.app import create_app
from trimset_server.config import Settings
from trimset_server.migrate import run_migrations

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

REQUEST = {
    "model": "jev-latest",
    "state": {"refund_id": "rf-1001", "amount": 129.5},
    "questions": {
        "refund_fraud": {"type": "noul", "instructions": "Is this refund request fraudulent?"},
        "team": {
            "type": "choice",
            "instructions": "Which team should handle this ticket?",
            "criteria": {"billing": None, "shipping": None, "returns": None, "tech": None},
        },
    },
}
RESPONSE = {
    "model": "jev-1.14.0",
    "answers": {
        "refund_fraud": {"type": "noul", "noul": 0.83},
        "team": {
            "type": "choice",
            "choice": "billing",
            "confidence": 0.7,
            "probabilities": {"billing": 0.7, "shipping": 0.1, "returns": 0.1, "tech": 0.1},
        },
    },
}


async def _project(dsn: str, settings: dict[str, Any] | None = None) -> tuple[str, Any]:
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        pid = await keys.create_project(conn, "acme")
        if settings:
            await conn.execute(
                "UPDATE projects SET settings = $2::jsonb WHERE project_id = $1",
                pid,
                json.dumps(settings),
            )
        _, key = await keys.create_key(conn, pid, "ingest", "test")
        return key, pid
    finally:
        await conn.close()


def _batch(
    ts: datetime | None = None, *, request: dict[str, Any] = REQUEST, **extra: Any
) -> dict[str, Any]:
    ex = parse_exchange(request, RESPONSE, "systemone")
    records = to_records(ex, ts=ts or datetime.now(UTC), request_id=uuid7(), **extra)
    return {"questions": {d.question_id: d.definition for d in ex.decisions}, "records": records}


def _post(c: TestClient, key: str, body: dict[str, Any]) -> Any:
    return c.post("/v1/decisions:log", json=body, headers={"Authorization": f"Bearer {key}"})


async def _rows(dsn: str, sql: str, *args: Any) -> list[Any]:
    conn = await asyncpg.connect(dsn)
    try:
        return list(await conn.fetch(sql, *args))
    finally:
        await conn.close()


@pytest.mark.req("FR-01")
@pytest.mark.req("FR-03")
async def test_a_batch_is_stored_once_with_its_questions_and_models(dsn: str) -> None:
    key, _ = await _project(dsn)
    body = _batch(tags={"channel": "web"}, external_ref="rf-1001", latency_ms=41, cost_usd=0.002)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        r = _post(c, key, body)
        assert (r.status_code, r.json()) == (200, {"stored": 2, "duplicates": 0, "rejected": 0})
        again = _post(c, key, body)  # a retry: same ids, same ts
        assert again.json() == {"stored": 0, "duplicates": 2, "rejected": 0}

    decisions = await _rows(dsn, "SELECT * FROM decisions ORDER BY question_id")
    assert len(decisions) == 2
    by_type = {d["type"]: d for d in decisions}
    assert by_type["noul"]["probs"] == pytest.approx([0.83])
    assert by_type["choice"]["probs"] == pytest.approx([0.7, 0.1, 0.1, 0.1])
    assert by_type["noul"]["external_ref"] == "rf-1001"
    assert json.loads(by_type["noul"]["tags"]) == {"channel": "web"}
    assert by_type["noul"]["cost_usd"] == pytest.approx(0.001)  # half of the request

    questions = await _rows(dsn, "SELECT name, type FROM questions ORDER BY name")
    assert [(q["name"], q["type"]) for q in questions] == [
        ("refund_fraud", "noul"),
        ("team", "choice"),
    ]
    models = await _rows(dsn, "SELECT model_requested, model_served FROM model_versions")
    assert [tuple(m) for m in models] == [("jev-latest", "jev-1.14.0")]


@pytest.mark.req("FR-02")
async def test_bad_records_are_rejected_by_index_and_the_rest_stored(dsn: str) -> None:
    key, _ = await _project(dsn, {"retention_days": 30, "privacy": {"tag_allowlist": ["channel"]}})
    body = _batch()
    good = body["records"][0]
    bad = [
        good | {"decision_id": uuid7(), "probabilities": ["0.8"]},  # 1: shape
        good | {"decision_id": uuid7(), "question_id": "q_" + "f" * 32},  # 2: unknown question
        good | {"decision_id": uuid7(), "type": "choice", "probabilities": [0.5, 0.5]},  # 3: type
        body["records"][1]
        | {"decision_id": uuid7(), "probabilities": [0.5, 0.5, 0.5, 0.5]},  # 4: sum
        body["records"][1] | {"decision_id": uuid7(), "probabilities": [1.0]},  # 5: count
        good | {"decision_id": uuid7(), "ts": (datetime.now(UTC) - timedelta(days=40)).isoformat()},
        good | {"decision_id": uuid7(), "ts": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        good | {"decision_id": uuid7(), "tags": {"customer_email": "x"}},  # 8: not on the list
    ]
    body["records"] = [good, *bad]
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        r = _post(c, key, body)
    assert r.status_code == 207
    result = r.json()
    assert (result["stored"], result["rejected"]) == (1, 8)
    errors = {e["index"]: e for e in result["errors"]}
    assert sorted(errors) == list(range(1, 9))
    assert errors[2]["message"].startswith("unknown question")
    assert errors[3]["pointer"] == "/records/3/type"
    assert errors[4]["message"] == "probabilities must sum to 1"
    assert errors[8]["message"] == "tag key 'customer_email' is not on the project's allow-list"
    assert "x" not in json.dumps(errors[8])  # the tag value never comes back


@pytest.mark.req("FR-03")
async def test_a_definition_must_hash_to_its_question_id(dsn: str) -> None:
    key, _ = await _project(dsn)
    body = _batch()
    qid = body["records"][0]["question_id"]
    body["questions"][qid] = body["questions"][qid] | {"instructions": "Something else?"}
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        result = _post(c, key, body).json()
    assert result["rejected"] == 1
    assert "doesn't hash to its question_id" in result["errors"][0]["message"]


async def test_a_refusal_is_stored_without_probabilities(dsn: str) -> None:
    key, _ = await _project(dsn)
    body = _batch()
    body["records"][0] |= {"refusal": True, "probabilities": None}
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        assert _post(c, key, body).json()["stored"] == 2
    rows = await _rows(dsn, "SELECT probs FROM decisions WHERE refusal")
    assert [r["probs"] for r in rows] == [None]


@pytest.mark.req("FR-03")
async def test_a_name_moves_to_the_newest_wording(dsn: str) -> None:
    key, _ = await _project(dsn)
    reworded = json.loads(json.dumps(REQUEST))
    reworded["questions"]["refund_fraud"]["instructions"] = "Is this refund fraudulent?"
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        first, second = _batch(), _batch(request=reworded)
        assert _post(c, key, first).json()["stored"] == 2
        assert _post(c, key, second).json()["stored"] == 2
    old_id = first["records"][0]["question_id"]
    new_id = second["records"][0]["question_id"]
    rows = await _rows(
        dsn,
        "SELECT question_id, name, previous_question_id FROM questions "
        "WHERE question_id = ANY($1::text[])",
        [old_id, new_id],
    )
    by_id = {r["question_id"]: r for r in rows}
    assert by_id[old_id]["name"] is None
    assert by_id[new_id]["name"] == "refund_fraud"
    assert by_id[new_id]["previous_question_id"] == old_id


async def test_known_questions_need_no_definition_next_time(dsn: str) -> None:
    key, _ = await _project(dsn)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        assert _post(c, key, _batch()).json()["stored"] == 2
        later = _batch()
        later["questions"] = {}
        assert _post(c, key, later).json()["stored"] == 2

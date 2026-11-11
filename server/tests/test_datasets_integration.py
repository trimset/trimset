"""Loading one group's decisions and outcomes for a fit, against a real Postgres (CI)."""

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient

from trimset_server import keys
from trimset_server.app import create_app
from trimset_server.config import Settings
from trimset_server.datasets import label_index, load_items
from trimset_server.migrate import run_migrations
from trimset_server.records import QuestionIn
from trimset_stats import dataset
from trimset_tools.datagen import generate, send

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _send(
    dsn: str, scenario: str, days: float, per_day: int
) -> tuple[object, object, datetime]:
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        pid = await keys.create_project(conn, "demo")
        ingest = (await keys.create_key(conn, pid, "ingest", "datagen"))[1]
    finally:
        await conn.close()
    now = datetime.now(UTC)
    data = generate(scenario, seed=4, end=now, days=days, per_day=per_day)
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        send(data, c, ingest, now=now)
    return pid, data, now


@pytest.mark.req("FR-14")
@pytest.mark.req("FR-18")
async def test_a_refund_fraud_group_loads_splits_and_shows_its_starved_path(dsn: str) -> None:
    pid, data, now = await _send(dsn, "refund-fraud", days=120, per_day=40)
    conn = await asyncpg.connect(dsn)
    try:
        question, items = await load_items(
            conn,
            pid,
            data.question_id,
            "jev-1.14.0",
            since=now - timedelta(days=90),  # type: ignore[attr-defined]
        )
    finally:
        await conn.close()
    assert question.type == "noul"
    assert items and all(len(i.probs) == 2 and abs(sum(i.probs) - 1) < 1e-9 for i in items)
    assert all(i.ts >= (now - timedelta(days=90)).timestamp() for i in items)

    arrived = {o["external_ref"] for t, o in data.outcomes if t <= now}  # type: ignore[attr-defined]
    labeled = [i for i in items if i.label is not None]
    assert labeled and all(i.external_ref in arrived for i in labeled)
    assert all(i.labelled_at is not None and i.labelled_at >= i.ts for i in labeled)

    paths = {p.action: p for p in dataset.coverage(items, now=now.timestamp())}
    assert paths["BLOCK"].starved  # held refunds never see a chargeback
    assert not paths["ESCALATE"].starved  # the risk team reviews every one
    built = dataset.build(items, now=now.timestamp(), seed=1, minimum=50)
    assert built.labeled == len({i.external_ref for i in labeled})
    assert min(len(built.fit.ids), len(built.tune.ids), len(built.eval.ids)) > 0


@pytest.mark.req("FR-15")
async def test_a_choice_groups_labels_become_option_indices(dsn: str) -> None:
    pid, data, now = await _send(dsn, "ticket-triage", days=5, per_day=60)
    conn = await asyncpg.connect(dsn)
    try:
        question, items = await load_items(
            conn,
            pid,
            data.question_id,
            "jev-1.14.0",
            since=now - timedelta(days=30),  # type: ignore[attr-defined]
        )
        with pytest.raises(LookupError):
            await load_items(conn, pid, "q_" + "0" * 32, "jev-1.14.0", since=now)
    finally:
        await conn.close()
    assert [o.value for o in question.options or []] == ["billing", "shipping", "returns", "tech"]
    truth = {t["decision_id"]: t["label"] for t in data.truth}  # type: ignore[attr-defined]
    labeled = [i for i in items if i.label is not None]
    assert labeled
    for item in labeled:
        assert ["billing", "shipping", "returns", "tech"][item.label] == truth[item.decision_id]  # type: ignore[index]
    assert all(len(i.probs) == 4 for i in items)


def test_label_indices_follow_the_question() -> None:
    noul = QuestionIn.model_validate(
        {
            "schema": "question/1",
            "type": "noul",
            "instructions": "x",
            "noul": {"true": None, "false": None},
        }
    )
    choice = QuestionIn.model_validate(
        {"schema": "question/1", "type": "choice", "instructions": "x",
         "options": [{"value": True, "description": None}, {"value": "true", "description": None}]}
    )  # fmt: skip
    assert (label_index(noul, True), label_index(noul, False), label_index(noul, "yes")) == (
        1,
        0,
        None,
    )
    assert (label_index(choice, True), label_index(choice, "true"), label_index(choice, 1)) == (
        0,
        1,
        None,
    )

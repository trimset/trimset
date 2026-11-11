"""The recorder writes project by project, so one project's bad data never costs another."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from trimset_server import recorder as recorder_module
from trimset_server.recorder import Pending, Recorder
from trimset_server.records import RecordIn

pytestmark = pytest.mark.anyio
A, B = uuid.uuid4(), uuid.uuid4()


def _pending(project: uuid.UUID, poison: bool = False) -> Pending:
    record = RecordIn.model_validate(
        {
            "schema": "record/1",
            "decision_id": str(uuid.uuid4()),
            "ts": datetime.now(UTC).isoformat(),
            "question_id": "q_" + ("f" if poison else "0") * 32,
            "type": "noul",
            "model_requested": "m",
            "model_served": "m",
            "probabilities": [0.5],
        }
    )
    return Pending(project, record, {"type": "noul"}, None)


class FakeDatabase:
    def __init__(self) -> None:
        self.stored: list[Pending] = []
        self.down_for: set[uuid.UUID] = set()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[str]:
        yield "conn"

    async def store(
        self, conn: Any, project_id: uuid.UUID, records: Any, *rest: Any
    ) -> tuple[int, int]:
        if project_id in self.down_for:
            raise ConnectionResetError("lost the connection")
        rows = [r for _, r in records]
        if any(r.question_id.startswith("q_f") for r in rows):
            raise asyncpg.DataError("refused")
        self.stored.extend(rows)  # type: ignore[arg-type]
        return len(rows), 0


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    fake = FakeDatabase()
    monkeypatch.setattr(recorder_module, "store", fake.store)
    return fake


@pytest.mark.req("NFR-06")
async def test_one_projects_bad_record_costs_no_one_else(db: FakeDatabase) -> None:
    rec = Recorder(db.connect, capacity=100)
    good_a, poison_a, good_b = _pending(A), _pending(A, poison=True), _pending(B)
    rec.add([good_a, poison_a, good_b])
    assert await rec.flush_once() is True
    assert {r.decision_id for r in db.stored} == {
        good_a.record.decision_id,
        good_b.record.decision_id,
    }
    assert rec.refused == 1
    assert rec.pending == 0


@pytest.mark.req("NFR-06")
async def test_an_outage_requeues_only_the_projects_not_yet_written(db: FakeDatabase) -> None:
    rec = Recorder(db.connect, capacity=100)
    a, b = _pending(A), _pending(B)
    rec.add([a, b])
    db.down_for = {B}
    assert await rec.flush_once() is False
    assert [r.decision_id for r in db.stored] == [a.record.decision_id]
    assert rec.pending == 1  # only B waits

    db.down_for = set()
    assert await rec.flush_once() is True
    assert rec.pending == 0

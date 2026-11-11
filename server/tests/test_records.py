import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from trimset_server.records import QuestionIn, RecordIn

EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "03-api" / "examples"
RECORD = json.loads((EXAMPLES / "record.json").read_text())
QUESTION = json.loads((EXAMPLES / "question.json").read_text())


def test_the_contract_examples_are_accepted() -> None:
    record = RecordIn.model_validate(RECORD)
    assert record.ts.tzinfo is not None
    assert QuestionIn.model_validate(QUESTION).size() == 1


@pytest.mark.req("FR-02")
@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"probabilities": ["0.83"]}, "a probability sent as a string"),
        ({"probabilities": [True]}, "a boolean where a number belongs"),
        ({"probabilities": [1.5]}, "outside [0, 1]"),
        ({"probabilities": [0.5, 0.5]}, "two probabilities for a noul question"),
        ({"probabilities": []}, "no probabilities"),
        ({"refusal": True}, "a refusal that still has probabilities"),
        ({"probabilities": None}, "no probabilities and no refusal"),
        ({"ts": "2026-11-10T09:41:07"}, "a timestamp without a time zone"),
        ({"ts": 1762767667}, "a timestamp as a number"),
        ({"decision_id": "not-a-uuid"}, "a malformed id"),
        ({"question_id": "q_123"}, "a malformed question_id"),
        ({"role": "shadow"}, "a shadow without a primary"),
        ({"shadow_of": "019a6f4e-3b2c-7d41-8e5f-2a1b3c4d5e6f"}, "a primary that names one"),
        ({"tags": {"bad key!": "x"}}, "a tag key with spaces"),
        ({"tags": {f"k{i}": "v" for i in range(21)}}, "21 tags"),
        ({"latency_ms": -1}, "a negative latency"),
        ({"surprise": 1}, "a field the schema doesn't have"),
        ({"schema": "record/2"}, "another schema version"),
    ],
)
def test_bad_records_are_refused(change: dict[str, Any], why: str) -> None:
    with pytest.raises(ValidationError):
        RecordIn.model_validate(RECORD | change)


def test_a_refusal_without_probabilities_is_fine() -> None:
    record = RecordIn.model_validate(RECORD | {"refusal": True, "probabilities": None})
    assert record.probabilities is None


@pytest.mark.parametrize(
    "question",
    [
        QUESTION | {"options": [{"value": "a", "description": None}]},  # noul with options too
        {k: v for k, v in QUESTION.items() if k != "noul"},  # noul without `noul`
        {"schema": "question/1", "type": "choice", "instructions": "x", "options": []},
        {
            "schema": "question/1",
            "type": "choice",
            "instructions": "x",
            "options": [{"value": 1, "description": None}],
        },
        {
            "schema": "question/1",
            "type": "score",
            "instructions": "x",
            "levels": [{"label": None, "description": None}] * 11,
        },
        {
            "schema": "question/1",
            "type": "noul",
            "noul": {"true": None, "false": None},
        },  # no instructions
    ],
)
def test_bad_question_definitions_are_refused(question: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        QuestionIn.model_validate(question)


def test_question_sizes() -> None:
    choice = {
        "schema": "question/1",
        "type": "choice",
        "instructions": "x",
        "options": [{"value": "a", "description": None}, {"value": True, "description": None}],
    }
    score = {
        "schema": "question/1",
        "type": "score",
        "instructions": "x",
        "levels": [{"label": None, "description": "low"}] * 3,
    }
    assert QuestionIn.model_validate(choice).size() == 2
    assert QuestionIn.model_validate(score).size() == 3

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from trimset_server.records import ActionIn, OutcomeIn, QuestionIn, RecordIn, fits

EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "03-api" / "examples"
RECORD = json.loads((EXAMPLES / "record.json").read_text())
QUESTION = json.loads((EXAMPLES / "question.json").read_text())
OUTCOME = json.loads((EXAMPLES / "outcome.json").read_text())


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


@pytest.mark.parametrize("field", ["latency_ms", "cost_usd"])
def test_numbers_beyond_any_real_use_are_refused(field: str) -> None:
    with pytest.raises(ValidationError):
        RecordIn.model_validate(RECORD | {field: 1e40})


def _question(**shape: Any) -> QuestionIn:
    kind = {"noul": "noul", "options": "choice", "levels": "score"}[next(iter(shape))]
    return QuestionIn.model_validate(
        {"schema": "question/1", "type": kind, "instructions": "x"} | shape
    )


NOUL = _question(noul={"true": None, "false": None})
CHOICE = _question(options=[{"value": "billing", "description": None},
                            {"value": True, "description": None}])  # fmt: skip
SCORE = _question(levels=[{"label": None, "description": None}] * 3)


@pytest.mark.req("FR-10")
@pytest.mark.parametrize(
    ("question", "label", "fits_it"),
    [
        (NOUL, True, True),
        (NOUL, False, True),
        (NOUL, "true", False),
        (NOUL, 1, False),
        (CHOICE, "billing", True),
        (CHOICE, True, True),
        (CHOICE, "True", False),
        (CHOICE, "sales", False),
        (SCORE, 0, True),
        (SCORE, 2, True),
        (SCORE, 3, False),
        (SCORE, -1, False),
        (SCORE, True, False),  # a boolean is not a level index
    ],
)
def test_a_label_fits_its_question(question: QuestionIn, label: Any, fits_it: bool) -> None:
    assert fits(question, label) is fits_it


def test_the_outcome_example_is_accepted() -> None:
    o = OutcomeIn.model_validate(OUTCOME)
    assert (o.external_ref, o.question, o.label) == ("rf-1001", "refund_fraud", True)


@pytest.mark.req("FR-10")
@pytest.mark.parametrize(
    "change",
    [
        {"question": None},  # a reference without its question
        {"decision_id": "019b2c11-8f00-7e2a-b1c3-d4e5f6a7b8c9"},  # two references
        {"external_ref": None, "question": None},  # no reference
        {"label": 0.5},
        {"label": None},
        {"label": ""},
        {"source": "guess"},
        {"observed_at": "2026-12-20T14:02:00"},
        {"surprise": 1},
    ],
)
def test_bad_outcomes_are_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OutcomeIn.model_validate({k: v for k, v in (OUTCOME | change).items() if v is not None})


def test_an_outcome_can_name_its_decision_by_id_alone() -> None:
    by_id = {k: v for k, v in OUTCOME.items() if k not in ("external_ref", "question")}
    by_id["decision_id"] = "019b2c11-8f00-7e2a-b1c3-d4e5f6a7b8c9"
    assert OutcomeIn.model_validate(by_id).decision_id is not None


@pytest.mark.req("FR-33")
@pytest.mark.parametrize(
    ("change", "ok"),
    [
        ({}, True),
        ({"action_taken": "SELECT:billing", "reason": "stale_policy"}, True),
        ({"action_taken": "MAYBE"}, False),
        ({"reason": "because"}, False),
        ({"policy_version": 0}, False),
        ({"policy_fingerprint": "abc"}, False),
        ({"extra": 1}, False),
    ],
)
def test_action_reports(change: dict[str, Any], ok: bool) -> None:
    report = {
        "decision_id": "019b2c11-8f00-7e2a-b1c3-d4e5f6a7b8c9",
        "action_taken": "ACCEPT",
        "action_source": "policy",
    } | change
    if ok:
        ActionIn.model_validate(report)
    else:
        with pytest.raises(ValidationError):
            ActionIn.model_validate(report)

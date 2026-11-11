"""wire-formats.md, checked on the vendors' own documented examples."""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trimset.wire import (
    Usage,
    WireError,
    parse_exchange,
    request_cost,
    to_records,
    uuid7,
)

API = Path(__file__).resolve().parents[3] / "docs" / "03-api"

# Ollaya's documented /v1/systemone example (docs/api.md §8.1), plus a score question.
SYSTEMONE_REQUEST = {
    "model": "laya",
    "state": {"subject": "Invoice", "message": "Can I get an invoice for last month?"},
    "questions": {
        "intent": {
            "type": "choice",
            "instructions": "What does the customer want?",
            "criteria": {
                "invoice": "Needs an invoice or receipt",
                "refund": "Wants money back",
                "other": "Anything else",
            },
        },
        "billing": {"type": "noul", "instructions": "Is this message about billing?"},
        "urgency": {
            "type": "score",
            "instructions": "How urgent is it?",
            "criteria": ["Can wait", "Needs attention this week", "Needs attention today"],
        },
    },
}
SYSTEMONE_RESPONSE = {
    "model": "laya:en",
    "answers": {
        "intent": {
            "type": "choice",
            "choice": "invoice",
            "confidence": 0.8995,
            "probabilities": {"invoice": 0.933, "refund": 0.021, "other": 0.046},
        },
        "billing": {"type": "noul", "noul": 0.9641},
        "urgency": {
            "type": "score",
            "score": 1.1982,
            "confidence": 0.3418,
            "legend": {
                "0": "Can wait",
                "1": "Needs attention this week",
                "2": "Needs attention today",
            },
            "probabilities": {"0": 0.1203, "1": 0.5612, "2": 0.3185},
        },
    },
    "usage": {"input_tokens": 71, "output_tokens": 0},
}

OPENAI_REQUEST = {
    "model": "gpt-6-luna",
    "input": "The parcel arrived crushed and the screen is cracked.",
    "questions": [
        {"type": "predicate", "name": "damaged", "instructions": "Is the item damaged?"},
        {
            "type": "choice",
            "name": "ok",
            "instructions": "Approve a refund?",
            "choices": [{"value": True}, {"value": "true", "description": "the word"}],
        },
        {
            "type": "score",
            "name": "severity",
            "instructions": "How bad is it?",
            "levels": [{"label": "minor"}, {"label": "major"}],
        },
        {"type": "predicate", "name": "fraud", "instructions": "Is this fraud?"},
    ],
}
OPENAI_RESPONSE = {
    "model": "gpt-6-luna",
    "answers": [
        {"type": "predicate", "name": "damaged", "probability": 0.95},
        {
            "type": "choice",
            "name": "ok",
            "choice": True,
            "confidence": 0.6,
            "probabilities": [
                {"value": "true", "probability": 0.2},
                {"value": True, "probability": 0.8},
            ],
        },
        {
            "type": "score",
            "name": "severity",
            "score": 0.7,
            "confidence": 0.4,
            "probabilities": [
                {"label": "major", "probability": 0.7, "value": 1},
                {"label": "minor", "probability": 0.3, "value": 0},
            ],
        },
        {"type": "refusal", "name": "fraud"},
    ],
    "usage": {
        "input_tokens": 1000,
        "output_tokens": 4,
        "total_tokens": 1004,
        "input_tokens_details": {"cached_tokens": 400, "cache_write_tokens": 0},
    },
}


def test_systemone_answers_come_out_in_question_order_with_ordered_probabilities() -> None:
    ex = parse_exchange(SYSTEMONE_REQUEST, SYSTEMONE_RESPONSE, "systemone")
    assert (ex.model_requested, ex.model_served) == ("laya", "laya:en")
    assert ex.state == SYSTEMONE_REQUEST["state"]
    assert [d.name for d in ex.decisions] == ["intent", "billing", "urgency"]
    intent, billing, urgency = ex.decisions
    assert intent.probabilities == [0.933, 0.021, 0.046]  # criteria order, not answer order
    assert billing.probabilities == [0.9641]
    assert urgency.probabilities == [0.1203, 0.5612, 0.3185]
    assert ex.skipped == []
    assert ex.usage == Usage(input_tokens=71, cached_input_tokens=0, output_tokens=0)


@pytest.mark.req("FR-02")
def test_ollamas_documented_sum_of_0_9999_is_accepted() -> None:
    request = {
        "model": "nimble",
        "state": "Our checkout has returned 500 errors since 9am.",
        "questions": {
            "label": {
                "type": "choice",
                "instructions": "Which label fits this ticket?",
                "criteria": {
                    "billing": "Payments and refunds",
                    "bug": "Software errors",
                    "account": "Login and account access",
                },
            }
        },
    }
    response = {
        "model": "nimble",
        "answers": {
            "label": {
                "type": "choice",
                "choice": "bug",
                "confidence": 0.8906,
                "probabilities": {"billing": 0.0125, "bug": 0.9781, "account": 0.0093},
            }
        },
        "usage": {"input_tokens": 174, "output_tokens": 1},
    }
    ex = parse_exchange(request, response, "systemone")
    assert ex.decisions[0].probabilities == [0.0125, 0.9781, 0.0093]


def test_openai_answers_match_by_position_and_value_type() -> None:
    ex = parse_exchange(OPENAI_REQUEST, OPENAI_RESPONSE, "openai-decisions")
    damaged, ok, severity, fraud = ex.decisions
    assert damaged.probabilities == [0.95]
    assert ok.probabilities == [0.8, 0.2]  # true then "true", as the question lists them
    assert severity.probabilities == [0.3, 0.7]  # level 0 first
    assert fraud.refusal is True and fraud.probabilities is None
    assert ex.state == OPENAI_REQUEST["input"]
    assert ex.usage == Usage(input_tokens=1000, cached_input_tokens=400, output_tokens=4)


@pytest.mark.req("FR-03")
def test_the_same_question_in_both_formats_gets_the_same_id() -> None:
    s1 = parse_exchange(
        {
            "model": "m",
            "state": "s",
            "questions": {"damaged": {"type": "noul", "instructions": "Is the item damaged?"}},
        },
        {"model": "m", "answers": {"damaged": {"type": "noul", "noul": 0.5}}},
        "systemone",
    )
    oa = parse_exchange(OPENAI_REQUEST, OPENAI_RESPONSE, "openai-decisions")
    assert s1.decisions[0].question_id == oa.decisions[0].question_id


def _with_answer(name: str, answer: Any) -> dict[str, Any]:
    response = json.loads(json.dumps(SYSTEMONE_RESPONSE))
    response["answers"][name] = answer
    return response


@pytest.mark.req("FR-02")
@pytest.mark.parametrize(
    ("name", "answer", "reason"),
    [
        (
            "intent",
            {"type": "choice", "probabilities": {"invoice": 0.9, "refund": 0.1}},
            "don't match",
        ),
        (
            "intent",
            {"type": "choice", "probabilities": {"invoice": 0.5, "refund": 0.2, "other": 0.2}},
            "sum to 1",
        ),
        ("intent", {"type": "noul", "noul": 0.5}, "not a choice answer"),
        ("billing", {"type": "noul", "noul": 1.2}, "outside [0, 1]"),
        ("billing", {"type": "noul", "noul": True}, "not a number"),
        ("billing", {"type": "noul", "noul": float("nan")}, "not a number"),
        ("urgency", {"type": "score", "probabilities": {"0": 0.5, "1": 0.5}}, "don't match"),
        (
            "urgency",
            {"type": "score", "probabilities": {"0": 0.2, "1": 0.3, "2": 0.4, "x": 0.1}},
            "don't match",
        ),
        ("billing", None, "no answer"),
    ],
)
def test_bad_answers_are_skipped_with_a_reason_never_guessed(
    name: str, answer: Any, reason: str
) -> None:
    response = _with_answer(name, answer)
    if answer is None:
        del response["answers"][name]
    ex = parse_exchange(SYSTEMONE_REQUEST, response, "systemone")
    assert name not in [d.name for d in ex.decisions]
    assert len(ex.decisions) == 2
    ((skipped, why),) = ex.skipped
    assert skipped == name and reason in why


def test_a_bad_question_is_skipped_but_the_rest_still_count() -> None:
    request = json.loads(json.dumps(SYSTEMONE_REQUEST))
    request["questions"]["box"] = {"type": "box", "instructions": "Where is the logo?"}
    ex = parse_exchange(request, SYSTEMONE_RESPONSE, "systemone")
    assert len(ex.decisions) == 3
    assert ex.skipped == [("box", "unsupported question type 'box'")]


def test_openai_answer_with_a_mismatched_value_type_is_skipped() -> None:
    response = json.loads(json.dumps(OPENAI_RESPONSE))
    response["answers"][1]["probabilities"] = [
        {"value": "true", "probability": 0.2},
        {"value": "True", "probability": 0.8},  # a string, not the boolean true
    ]
    ex = parse_exchange(OPENAI_REQUEST, response, "openai-decisions")
    assert ("ok", "choice probabilities don't match the question's choice options") in ex.skipped


@pytest.mark.parametrize(
    ("request_", "response", "fmt"),
    [
        ({"questions": {}}, {"model": "m", "answers": {}}, "systemone"),
        ({"model": "m", "questions": []}, {"model": "m", "answers": {}}, "systemone"),
        ({"model": "m", "questions": {}}, {"model": "m", "answers": []}, "openai-decisions"),
        ({"model": "m", "questions": {}}, {"model": "m", "answers": {}}, "other"),
        ([], {}, "systemone"),
    ],
)
def test_an_unusable_exchange_raises(request_: Any, response: Any, fmt: Any) -> None:
    with pytest.raises(WireError):
        parse_exchange(request_, response, fmt)


@pytest.mark.req("FR-07")
def test_cost_uses_cached_prices_and_is_split_across_questions() -> None:
    usage = Usage(input_tokens=1000, cached_input_tokens=400, output_tokens=4)
    price = {"input": 0.10, "cached_input": 0.025, "output": 0.0}
    cost = request_cost(usage, price)
    assert cost == pytest.approx((600 * 0.10 + 400 * 0.025) / 1e6)
    assert request_cost(usage, {"input": 0.1}) == pytest.approx(1000 * 0.1 / 1e6)
    assert request_cost(usage, None) is None

    ex = parse_exchange(OPENAI_REQUEST, OPENAI_RESPONSE, "openai-decisions")
    records = to_records(ex, ts=datetime.now(UTC), cost_usd=cost)
    assert sum(r["cost_usd"] for r in records) == pytest.approx(cost)


def test_uuid7_carries_version_variant_and_time_order() -> None:
    ids = [uuid7() for _ in range(50)]
    parsed = [uuid.UUID(i) for i in ids]
    assert all(u.version == 7 and u.variant == uuid.RFC_4122 for u in parsed)
    assert len(set(ids)) == 50
    ms = [u.int >> 80 for u in parsed]
    assert ms == sorted(ms)  # the 48-bit millisecond prefix never goes backwards


@pytest.mark.req("FR-01")
def test_records_are_valid_record_1() -> None:
    jsonschema = pytest.importorskip("jsonschema")  # not installed in the Python 3.11 job
    from referencing import Registry, Resource

    schemas = {p.name: json.loads(p.read_text()) for p in (API / "schemas").glob("*.json")}
    registry = Registry().with_resources(
        (name, Resource.from_contents(s)) for name, s in schemas.items()
    )
    validator = jsonschema.Draft202012Validator(schemas["record.schema.json"], registry=registry)

    for request, response, fmt in [
        (SYSTEMONE_REQUEST, SYSTEMONE_RESPONSE, "systemone"),
        (OPENAI_REQUEST, OPENAI_RESPONSE, "openai-decisions"),
    ]:
        ex = parse_exchange(request, response, fmt)  # type: ignore[arg-type]
        records = to_records(
            ex,
            ts=datetime.now(UTC),
            request_id=uuid7(),
            state_hash="0" * 64,
            latency_ms=12.5,
            cost_usd=0.001,
            tags={"channel": "web"},
            external_ref="rf-1001",
        )
        for record in records:
            errors = list(validator.iter_errors(record))
            assert not errors, [e.message for e in errors]


def test_decision_ids_can_be_supplied_one_per_decision() -> None:
    ex = parse_exchange(SYSTEMONE_REQUEST, SYSTEMONE_RESPONSE, "systemone")
    ids = [uuid7() for _ in ex.decisions]
    assert [r["decision_id"] for r in to_records(ex, ts=datetime.now(UTC), decision_ids=ids)] == ids
    with pytest.raises(ValueError, match="one decision id per decision"):
        to_records(ex, ts=datetime.now(UTC), decision_ids=ids[:1])

"""Turn decision-model traffic into trimset records (docs/03-api/wire-formats.md).

Two formats: "systemone" (TypeSafe Jev, Ollaya, Ollama) and "openai-decisions" (OpenAI).
No I/O here: the proxy and the SDK share it. Anything malformed is skipped with a reason,
never guessed.
"""

import math
import os
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from trimset.hashing import question_id

Format = Literal["systemone", "openai-decisions"]
SUM_TOLERANCE = 1e-3  # vendors round to 4 decimals (wire-formats.md §3)
_OPENAI_TYPES = {"predicate": "noul", "choice": "choice", "score": "score"}


class WireError(ValueError):
    """The traffic doesn't match its format."""


@dataclass(frozen=True)
class Decision:
    """One answered question."""

    name: str | None
    question_id: str
    definition: dict[str, Any]
    probabilities: list[float] | None  # None when the model refused
    refusal: bool = False


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Exchange:
    """One request and its response, parsed."""

    format: Format
    model_requested: str
    model_served: str
    state: Any  # systemone `state`, OpenAI `input`: for hashing only, never stored
    decisions: list[Decision]
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (question, reason)
    usage: Usage = field(default_factory=Usage)


# ---------------------------------------------------------------- questions


def _instructions(q: Mapping[str, Any], name: str | None) -> Any:
    instructions = q.get("instructions")
    return instructions if instructions is not None else {"$name": name}


def question_from_systemone(name: str, q: Mapping[str, Any]) -> dict[str, Any]:
    """A question/1 definition from a /v1/systemone question (hashing.md §2)."""
    if not isinstance(q, Mapping):
        raise WireError("question is not an object")
    kind = q.get("type")
    d: dict[str, Any] = {
        "schema": "question/1",
        "type": kind,
        "instructions": _instructions(q, name),
    }
    criteria = q.get("criteria")
    if kind == "noul":
        criteria = criteria if isinstance(criteria, Mapping) else {}
        d["noul"] = {"true": criteria.get("true"), "false": criteria.get("false")}
    elif kind == "choice":
        if not isinstance(criteria, Mapping) or not criteria:
            raise WireError("choice question without criteria")
        d["options"] = [{"value": k, "description": v} for k, v in criteria.items()]
    elif kind == "score":
        if not isinstance(criteria, list) or not criteria:
            raise WireError("score question without criteria")
        d["levels"] = [{"label": None, "description": v} for v in criteria]
    else:
        raise WireError(f"unsupported question type {kind!r}")
    return d


def question_from_openai(q: Mapping[str, Any]) -> dict[str, Any]:
    """A question/1 definition from an OpenAI /v1/decisions question (hashing.md §2)."""
    if not isinstance(q, Mapping):
        raise WireError("question is not an object")
    kind = _OPENAI_TYPES.get(str(q.get("type")))
    if kind is None:
        raise WireError(f"unsupported question type {q.get('type')!r}")
    d: dict[str, Any] = {
        "schema": "question/1",
        "type": kind,
        "instructions": _instructions(q, q.get("name")),
    }
    try:
        if kind == "noul":
            d["noul"] = {"true": None, "false": None}
        elif kind == "choice":
            d["options"] = [
                {"value": c["value"], "description": c.get("description")} for c in q["choices"]
            ]
        else:
            d["levels"] = [
                {"label": lv["label"], "description": lv.get("description")} for lv in q["levels"]
            ]
    except (KeyError, TypeError) as e:
        raise WireError(f"malformed {kind} question") from e
    return d


# ---------------------------------------------------------------- answers


def _key(value: Any) -> tuple[str, Any]:
    """JSON equality: the boolean true and the string "true" are different options."""
    return (type(value).__name__, value)


def _number(p: Any) -> float:
    if isinstance(p, bool) or not isinstance(p, int | float) or not math.isfinite(p):
        raise WireError("a probability is not a number")
    if not 0 <= p <= 1:
        raise WireError("a probability is outside [0, 1]")
    return float(p)


def _probabilities(definition: Mapping[str, Any], answer: Any, fmt: Format) -> list[float]:
    kind = definition["type"]
    wanted = kind if fmt == "systemone" else ("predicate" if kind == "noul" else kind)
    if not isinstance(answer, Mapping) or answer.get("type") != wanted:
        raise WireError(f"answer is not a {wanted} answer")
    raw = answer.get("probabilities")

    if kind == "noul":
        return [_number(answer.get("noul" if fmt == "systemone" else "probability"))]

    if kind == "choice":
        keys = [_key(o["value"]) for o in definition["options"]]
        if fmt == "systemone":
            found = {_key(k): v for k, v in raw.items()} if isinstance(raw, Mapping) else None
        else:
            found = (
                {_key(i.get("value")): i.get("probability") for i in raw if isinstance(i, Mapping)}
                if isinstance(raw, list)
                else None
            )
    else:
        keys = [("int", i) for i in range(len(definition["levels"]))]
        if fmt == "systemone":
            found = (
                {("int", int(k)): v for k, v in raw.items() if str(k).isdigit()}
                if isinstance(raw, Mapping)
                else None
            )
        else:
            found = (
                {_key(i.get("value")): i.get("probability") for i in raw if isinstance(i, Mapping)}
                if isinstance(raw, list)
                else None
            )

    sent = len(raw) if isinstance(raw, Mapping | list) else -1
    if found is None or set(found) != set(keys) or len(found) != sent:
        raise WireError(f"{kind} probabilities don't match the question's {kind} options")
    probs = [_number(found[k]) for k in keys]
    if abs(sum(probs) - 1) > SUM_TOLERANCE:
        raise WireError("probabilities don't sum to 1")
    return probs


def _usage(response: Mapping[str, Any]) -> Usage:
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return Usage()

    def count(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    details = usage.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, Mapping) else 0
    return Usage(count(usage.get("input_tokens")), count(cached), count(usage.get("output_tokens")))


def parse_exchange(
    request: Mapping[str, Any], response: Mapping[str, Any], fmt: Format
) -> Exchange:
    """Every question in request order, with its answer. Raises WireError only when the
    exchange as a whole is unusable; a bad question or answer is skipped with a reason."""
    if not isinstance(request, Mapping) or not isinstance(response, Mapping):
        raise WireError("request and response must be JSON objects")
    model_requested, model_served = request.get("model"), response.get("model")
    if not isinstance(model_requested, str) or not isinstance(model_served, str):
        raise WireError("request or response has no model")

    questions, answers = request.get("questions"), response.get("answers")
    decisions: list[Decision] = []
    skipped: list[tuple[str, str]] = []
    pairs: list[tuple[str, str | None, Any, Any]] = []  # (label, name, question, answer)

    if fmt == "systemone":
        if not isinstance(questions, Mapping) or not isinstance(answers, Mapping):
            raise WireError("systemone questions and answers must be objects")
        pairs = [(str(n), str(n), q, answers.get(n)) for n, q in questions.items()]
        state = request.get("state")
    elif fmt == "openai-decisions":
        if not isinstance(questions, list) or not isinstance(answers, list):
            raise WireError("openai-decisions questions and answers must be arrays")
        for i, q in enumerate(questions):
            raw_name = q.get("name") if isinstance(q, Mapping) else None
            name = raw_name if isinstance(raw_name, str) else None
            pairs.append((name or f"#{i}", name, q, answers[i] if i < len(answers) else None))
        state = request.get("input")
    else:
        raise WireError(f"unknown format {fmt!r}")

    for label, name, q, answer in pairs:
        try:
            definition = (
                question_from_systemone(label, q) if fmt == "systemone" else question_from_openai(q)
            )
            qid = question_id(definition)
            if answer is None:
                raise WireError("no answer")
            refused = isinstance(answer, Mapping) and answer.get("type") == "refusal"
            if fmt == "openai-decisions" and refused:
                decisions.append(Decision(name, qid, definition, None, refusal=True))
                continue
            probs = _probabilities(definition, answer, fmt)
        except WireError as e:
            skipped.append((label, str(e)))
            continue
        decisions.append(Decision(name, qid, definition, probs))

    return Exchange(fmt, model_requested, model_served, state, decisions, skipped, _usage(response))


# ---------------------------------------------------------------- records


def request_cost(usage: Usage, price: Mapping[str, float] | None) -> float | None:
    """USD for one request, from per-million-token prices; None without a price."""
    if not price or "input" not in price:
        return None
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    total = (
        uncached * price["input"]
        + usage.cached_input_tokens * price.get("cached_input", price["input"])
        + usage.output_tokens * price.get("output", 0.0)
    )
    return total / 1_000_000


def uuid7() -> str:
    """A UUIDv7 (RFC 9562): sorts by creation time, so the server can find its partition."""
    ms = time.time_ns() // 1_000_000
    value = (ms & 0xFFFF_FFFF_FFFF) << 80 | int.from_bytes(os.urandom(10), "big")
    value = (value & ~(0xF << 76)) | (0x7 << 76)  # version 7
    value = (value & ~(0x3 << 62)) | (0x2 << 62)  # RFC variant
    return str(uuid.UUID(int=value))


def to_records(
    exchange: Exchange,
    *,
    ts: datetime,
    request_id: str | None = None,
    decision_ids: Sequence[str] | None = None,
    state_hash: str | None = None,
    latency_ms: float | None = None,
    cost_usd: float | None = None,
    tags: Mapping[str, str] | None = None,
    external_ref: str | None = None,
) -> list[dict[str, Any]]:
    """record/1 documents, one per decision. `cost_usd` is the whole request's cost; each
    decision gets an equal share (wire-formats.md §3)."""
    n = len(exchange.decisions)
    ids = list(decision_ids) if decision_ids is not None else [uuid7() for _ in range(n)]
    if len(ids) != n:
        raise ValueError("one decision id per decision")
    share = cost_usd / n if cost_usd is not None and n else None
    records = []
    for decision_id, d in zip(ids, exchange.decisions, strict=True):
        record: dict[str, Any] = {
            "schema": "record/1",
            "decision_id": decision_id,
            "request_id": request_id,
            "ts": ts.isoformat(),
            "question_id": d.question_id,
            "question_name": d.name,
            "type": d.definition["type"],
            "format": exchange.format,
            "model_requested": exchange.model_requested,
            "model_served": exchange.model_served,
            "probabilities": d.probabilities,
            "refusal": d.refusal,
            "latency_ms": latency_ms,
            "cost_usd": share,
            "tags": dict(tags or {}),
            "external_ref": external_ref,
            "state_hash": state_hash,
        }
        if state_hash is not None:
            record["state_hash_alg"] = "hmac-sha256/1"
        records.append(record)
    return records

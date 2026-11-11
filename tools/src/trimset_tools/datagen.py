"""Synthetic decisions, outcomes and ground truth for the six use cases (FR-60).

    trimset-datagen refund-fraud --seed 1 --days 30 --out data/
    TRIMSET_INGEST_KEY=tsi_... trimset-datagen ticket-triage --url http://localhost:8800

How a decision is made up (docs/01-requirements.md §4):
- The true answer distribution q is drawn around the scenario's base rates; the true
  label is drawn from q. A model that reports q is perfectly calibrated.
- The model reports p = softmax(slope · log q + bias on the scenario's tilt class): the
  planted miscalibration. Slope 1 and bias 0 report q itself.
- From `drift_at` (a share of the timeline) the tilt class becomes `drift` times as
  likely. From `swap_at` the served model changes, silently, to one with `swap_to`'s
  calibration.
- The app acts with today's hard-coded thresholds, and each outcome channel (a
  chargeback, a reviewer, a webhook) decides whether a label comes back, which, and when.
  `missing` drops that share of the labels; `delay_scale` stretches every delay.

Everything comes from the seed and `end`, so the same arguments give the same data.
"""

import argparse
import json
import math
import os
import random
import sys
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2

from trimset.hashing import question_id
from trimset.wire import question_from_systemone

HOUR, DAY = 3600.0, 86400.0


@dataclass(frozen=True)
class Plants:
    """What is wrong with the model or the world. The defaults plant nothing."""

    slope: float = 1.0  # < 1 underconfident, > 1 overconfident
    bias: float = 0.0  # added to the tilt class's log-odds
    drift_at: float | None = None
    drift: float = 1.0  # how many times as likely the tilt class becomes
    swap_at: float | None = None
    swap_to: tuple[float, float] = (0.6, 0.8)  # the new model's (slope, bias)
    missing: float = 0.0  # share of labels that never come back
    delay_scale: float = 1.0

    def __post_init__(self) -> None:
        for name in ("drift_at", "swap_at"):
            at = getattr(self, name)
            if at is not None and not 0 <= at <= 1:
                raise ValueError(f"{name} is a share of the timeline, from 0 to 1")
        if not 0 <= self.missing <= 1:
            raise ValueError("missing is a share, from 0 to 1")
        if self.slope <= 0 or self.drift <= 0 or self.delay_scale <= 0 or self.swap_to[0] <= 0:
            raise ValueError("slope, drift, delay_scale and the swap slope must be above 0")


@dataclass(frozen=True)
class Context:
    """What an outcome channel knows about one decision."""

    label: Any
    p: list[float]  # the model's probabilities, one per class
    action: str
    audit: bool
    rng: random.Random


Outcome = tuple[float, Any, str]  # (seconds after the decision, label, source)


@dataclass(frozen=True)
class Scenario:
    key: str
    use_case: str
    name: str  # the question's name in systemone
    question: dict[str, Any]  # as a systemone request carries it
    classes: list[Any]  # labels, in the order of `p`; a noul question is [False, True]
    base: list[float]  # how often each label is true
    concentration: float  # lower: q nearer 0 and 1, an easier task
    tilt: int  # the class that bias and drift act on
    act: Callable[[list[float]], str]  # the app's current hard-coded rule
    outcomes: Callable[[Context], list[Outcome]]
    by_ref: bool = False  # outcomes name the decision by external_ref, not decision_id
    audit_rate: float = 0.0  # share of accepted decisions sent to review anyway
    default_rule: dict[str, Any] | None = None  # question settings (FR-13)
    models: tuple[str, str, str] = ("jev-latest", "jev-1.14.0", "jev-1.15.0")
    tags: dict[str, Sequence[str]] = field(default_factory=dict)


def _band(lo: float, hi: float, below: str, above: str) -> Callable[[list[float]], str]:
    """A noul rule: under lo, `below`; from hi, `above`; between them, ESCALATE."""

    def act(p: list[float]) -> str:
        return below if p[1] < lo else above if p[1] >= hi else "ESCALATE"

    return act


def _days(rng: random.Random, lo: float, hi: float) -> float:
    return rng.uniform(lo, hi) * DAY


# ---------------------------------------------------------------- outcome channels


def _refund_outcomes(c: Context) -> list[Outcome]:
    """Chargebacks 30-90 days after a paid fraud; the risk team on escalations and the
    1% audit; held refunds almost never learn the truth. "No chargeback in 120 days"
    is the server's default rule, not sent from here."""
    if c.action == "ESCALATE" or c.audit:
        return [(c.rng.uniform(2, 48) * HOUR, c.label, "human_review")]
    if c.action == "ACCEPT" and c.label and c.rng.random() < 0.9:
        return [(_days(c.rng, 30, 90), True, "downstream")]
    return []


def _triage_outcomes(c: Context) -> list[Outcome]:
    """The team that closed the ticket, from the help desk's webhook, hours later."""
    if c.rng.random() < 0.95:
        return [(c.rng.uniform(1, 72) * HOUR, c.label, "downstream")]
    return []


def _routing_outcomes(c: Context) -> list[Outcome]:
    """An LLM judge grades 2% with the true level, within minutes. A thumbs-down or a
    retry after a small-model answer to a hard request is recorded as level 2, the least
    it tells us."""
    out: list[Outcome] = []
    if c.rng.random() < 0.02:
        out.append((c.rng.uniform(1, 30) * 60, c.label, "downstream"))
    if c.action == "ACCEPT" and c.label >= 2 and c.rng.random() < 0.6:
        out.append((c.rng.uniform(0.5, 10) * 60, 2, "downstream"))
    return out


def _pii_outcomes(c: Context) -> list[Outcome]:
    """The privacy team reviews 5% of passed prompts weekly; the leak scanner flags half
    of the personal data that got through, days later."""
    if c.action == "ACCEPT" and c.rng.random() < 0.05:
        return [(_days(c.rng, 1, 7), c.label, "human_review")]
    if c.action == "ACCEPT" and c.label and c.rng.random() < 0.5:
        return [(_days(c.rng, 1, 5), True, "downstream")]
    return []


def _tool_outcomes(c: Context) -> list[Outcome]:
    """A human approves or denies escalated calls within minutes. An unsafe call that ran
    shows up as an incident, rarely and late. The label is "safe"."""
    if c.action == "ESCALATE":
        return [(c.rng.uniform(1, 30) * 60, c.label, "human_review")]
    if c.action == "ACCEPT" and not c.label and c.rng.random() < 0.7:
        return [(_days(c.rng, 1, 21), False, "downstream")]
    return []


def _moderation_outcomes(c: Context) -> list[Outcome]:
    """Moderators decide escalated listings within hours; users report a third of the
    serious ones that went live, days later."""
    if c.action == "ESCALATE":
        return [(c.rng.uniform(0.5, 12) * HOUR, c.label, "human_review")]
    if c.action == "ACCEPT" and c.label >= 2 and c.rng.random() < 0.3:
        return [(_days(c.rng, 1, 10), c.label, "downstream")]
    return []


def _triage_act(p: list[float]) -> str:
    teams = ["billing", "shipping", "returns", "tech"]
    top = max(range(4), key=p.__getitem__)
    return f"SELECT:{teams[top]}" if p[top] >= 0.8 else "ESCALATE"


def _moderation_act(p: list[float]) -> str:
    level = max(range(4), key=p.__getitem__)
    return "BLOCK" if level == 3 else "ACCEPT" if level == 0 else "ESCALATE"


SCENARIOS = {
    s.key: s
    for s in (
        Scenario(
            key="refund-fraud",
            use_case="UC-1",
            name="refund_fraud",
            question={"type": "noul", "instructions": "Is this refund request fraudulent?"},
            classes=[False, True],
            base=[0.97, 0.03],
            concentration=1.0,
            tilt=1,
            act=_band(0.2, 0.8, "ACCEPT", "BLOCK"),
            outcomes=_refund_outcomes,
            by_ref=True,
            audit_rate=0.01,
            default_rule={"after_days": 120, "label": False},
            tags={"channel": ("web", "app")},
        ),
        Scenario(
            key="ticket-triage",
            use_case="UC-2",
            name="team",
            question={
                "type": "choice",
                "instructions": "Which team should handle this ticket?",
                "criteria": {
                    "billing": "Invoices, charges and payment methods",
                    "shipping": "Where an order is, and delivery problems",
                    "returns": "Sending items back and refunds",
                    "tech": "The website or the app not working",
                },
            },
            classes=["billing", "shipping", "returns", "tech"],
            base=[0.3, 0.35, 0.25, 0.1],
            concentration=0.35,  # a sharp model: most tickets have a clear team
            tilt=2,
            act=_triage_act,
            outcomes=_triage_outcomes,
            by_ref=True,
            tags={"channel": ("email", "chat")},
        ),
        Scenario(
            key="model-routing",
            use_case="UC-3",
            name="difficulty",
            question={
                "type": "score",
                "instructions": "How hard is this request?",
                "criteria": ["Trivial", "Easy", "Hard", "Expert"],
            },
            classes=[0, 1, 2, 3],
            base=[0.35, 0.4, 0.18, 0.07],
            concentration=0.6,
            tilt=3,
            act=lambda p: "ACCEPT" if p[2] + p[3] < 0.3 else "ESCALATE",
            outcomes=_routing_outcomes,
            models=("router-latest", "router-2.3.0", "router-2.4.0"),
        ),
        Scenario(
            key="pii-guardrail",
            use_case="UC-4",
            name="personal_data",
            question={"type": "noul", "instructions": "Does this text contain personal data?"},
            classes=[False, True],
            base=[0.95, 0.05],
            concentration=1.0,
            tilt=1,
            act=_band(0.1, 0.7, "ACCEPT", "BLOCK"),
            outcomes=_pii_outcomes,
        ),
        Scenario(
            key="tool-approval",
            use_case="UC-5",
            name="safe_to_run",
            question={
                "type": "noul",
                "instructions": "Is this tool call safe to run without a human?",
            },
            classes=[False, True],
            base=[0.01, 0.99],
            concentration=1.0,
            tilt=0,
            act=_band(0.5, 0.95, "BLOCK", "ACCEPT"),
            outcomes=_tool_outcomes,
        ),
        Scenario(
            key="content-moderation",
            use_case="UC-6",
            name="severity",
            question={
                "type": "score",
                "instructions": "How serious is the problem with this listing?",
                "criteria": ["None", "Mild", "Serious", "Remove"],
            },
            classes=[0, 1, 2, 3],
            base=[0.85, 0.08, 0.05, 0.02],
            concentration=1.0,
            tilt=3,
            act=_moderation_act,
            outcomes=_moderation_outcomes,
        ),
    )
}


# ---------------------------------------------------------------- generation


@dataclass
class Dataset:
    scenario: Scenario
    question_id: str
    definition: dict[str, Any]
    records: list[dict[str, Any]]  # record/1, oldest first
    outcomes: list[tuple[datetime, dict[str, Any]]]  # (arrives at, outcome/1), in arrival order
    truth: list[dict[str, Any]]  # {decision_id, label, q}: never sent, for experiments


def _uuid7(ts: datetime, rng: random.Random) -> str:
    """A version-7 UUID for `ts` whose random bits come from `rng`."""
    ms = int(ts.timestamp() * 1000)
    rand = rng.getrandbits(74)
    value = (ms << 80) | (0x7 << 76) | ((rand >> 62) << 64) | (0b10 << 62) | (rand & (2**62 - 1))
    return str(uuid.UUID(int=value))


def _uuid4(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _dirichlet(rng: random.Random, alpha: list[float]) -> list[float]:
    draws = [max(rng.gammavariate(a, 1.0), 1e-300) for a in alpha]
    total = sum(draws)
    return [max(d / total, 1e-12) for d in draws]


def _report(q: list[float], slope: float, bias: float, tilt: int) -> list[float]:
    logits = [slope * math.log(x) for x in q]
    logits[tilt] += bias
    top = max(logits)
    weights = [math.exp(x - top) for x in logits]
    p = [round(w / sum(weights), 6) for w in weights]
    biggest = p.index(max(p))
    p[biggest] = round(1 - (sum(p) - p[biggest]), 6)
    return p


def generate(
    scenario: Scenario | str,
    *,
    seed: int,
    end: datetime,
    days: float = 30,
    per_day: int = 200,
    plants: Plants = Plants(),  # noqa: B008 (frozen, never mutated)
) -> Dataset:
    s = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
    if end.tzinfo is None:
        raise ValueError("end needs a time zone")
    rng = random.Random(f"{s.key}:{seed}")  # noqa: S311 (test data)
    definition = question_from_systemone(s.name, s.question)
    qid = question_id(definition)
    n = max(1, round(days * per_day))
    start = end - timedelta(days=days)
    offsets = sorted(rng.uniform(0, days * DAY) for _ in range(n))

    records: list[dict[str, Any]] = []
    outcomes: list[tuple[datetime, dict[str, Any]]] = []
    truth: list[dict[str, Any]] = []
    for i, offset in enumerate(offsets):
        ts = start + timedelta(seconds=offset)
        share = offset / (days * DAY)
        base = list(s.base)
        if plants.drift_at is not None and share >= plants.drift_at:
            base[s.tilt] *= plants.drift
            base = [b / sum(base) for b in base]
        q = _dirichlet(rng, [s.concentration * b * len(base) for b in base])
        label = s.classes[rng.choices(range(len(q)), weights=q)[0]]
        swapped = plants.swap_at is not None and share >= plants.swap_at
        slope, bias = plants.swap_to if swapped else (plants.slope, plants.bias)
        p = _report(q, slope, bias, s.tilt)
        action = s.act(p)
        audit = action == "ACCEPT" and rng.random() < s.audit_rate
        if audit:  # sampled for review although the rule would accept it
            action = "ESCALATE"

        decision_id = _uuid7(ts, rng)
        ref = f"{s.key.split('-')[0]}-{seed}-{i:06d}"
        record: dict[str, Any] = {
            "schema": "record/1",
            "decision_id": decision_id,
            "ts": ts.isoformat(),
            "question_id": qid,
            "question_name": s.name,
            "type": definition["type"],
            "format": "systemone",
            "model_requested": s.models[0],
            "model_served": s.models[2 if swapped else 1],
            "probabilities": [p[1]] if definition["type"] == "noul" else p,
            "latency_ms": round(rng.uniform(40, 400), 1),
            "action_taken": action,
            "action_source": "audit" if audit else "default",
            "audit": audit,
            "tags": {k: rng.choice(v) for k, v in s.tags.items()},
        }
        if s.by_ref:
            record["external_ref"] = ref
        records.append(record)
        truth.append({"decision_id": decision_id, "label": label, "q": q})

        context = Context(label, p, action, audit, rng)
        for delay, value, source in s.outcomes(context):
            if rng.random() < plants.missing:
                continue
            arrives = ts + timedelta(seconds=delay * plants.delay_scale)
            outcome: dict[str, Any] = {
                "schema": "outcome/1",
                "outcome_id": _uuid4(rng),
                "label": value,
                "source": source,
                "observed_at": arrives.isoformat(),
            }
            outcome |= (
                {"external_ref": ref, "question": s.name}
                if s.by_ref
                else {"decision_id": decision_id}
            )
            outcomes.append((arrives, outcome))

    outcomes.sort(key=lambda pair: pair[0])
    return Dataset(s, qid, definition, records, outcomes, truth)


# ---------------------------------------------------------------- output


def write_jsonl(data: Dataset, out: Path) -> None:
    """questions.json, records.jsonl, outcomes.jsonl (with arrives_at) and truth.jsonl."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "questions.json").write_text(json.dumps({data.question_id: data.definition}) + "\n")

    def lines(name: str, rows: Iterator[Any]) -> None:
        with (out / name).open("w") as f:
            f.writelines(json.dumps(row) + "\n" for row in rows)

    lines("records.jsonl", iter(data.records))
    lines("outcomes.jsonl", ({"arrives_at": t.isoformat(), **o} for t, o in data.outcomes))
    lines("truth.jsonl", iter(data.truth))


def _chunks(rows: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def send(
    data: Dataset,
    client: httpx2.Client,
    ingest_key: str,
    *,
    admin_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Logs the decisions, then the outcomes that have arrived by `now`; with an admin
    key, sets the question's default rule first. Returns the server's counts."""
    now = now or datetime.now(UTC)
    totals = {"stored": 0, "duplicates": 0, "rejected": 0, "matched": 0, "held": 0}
    totals |= {"outcomes_rejected": 0, "not_yet_arrived": 0}
    ingest = {"Authorization": f"Bearer {ingest_key}"}

    for batch in _chunks(data.records, 500):
        body = {"questions": {data.question_id: data.definition}, "records": batch}
        r = client.post("/v1/decisions:log", json=body, headers=ingest)
        r.raise_for_status()
        for k in ("stored", "duplicates", "rejected"):
            totals[k] += r.json()[k]

    if admin_key and data.scenario.default_rule:
        r = client.patch(
            f"/v1/admin/questions/{data.scenario.name}",
            json={"settings": {"default_rule": data.scenario.default_rule}},
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        r.raise_for_status()

    arrived = [o for t, o in data.outcomes if t <= now]
    totals["not_yet_arrived"] = len(data.outcomes) - len(arrived)
    for batch in _chunks(arrived, 1000):
        r = client.post("/v1/outcomes", json={"outcomes": batch}, headers=ingest)
        r.raise_for_status()
        totals["matched"] += r.json()["matched"]
        totals["held"] += r.json()["held"]
        totals["outcomes_rejected"] += r.json()["rejected"]
    return totals


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="trimset-datagen",
        description="Synthetic decisions and outcomes for the six use cases (FR-60). "
        "Send them with --url (keys from TRIMSET_INGEST_KEY and TRIMSET_ADMIN_KEY), "
        "or write JSONL with --out.",
    )
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--days", type=float, default=30)
    parser.add_argument("--per-day", type=int, default=200)
    parser.add_argument("--end", type=datetime.fromisoformat, help="default: now")
    parser.add_argument("--slope", type=float, default=1.0)
    parser.add_argument("--bias", type=float, default=0.0)
    parser.add_argument("--drift-at", type=float)
    parser.add_argument("--drift", type=float, default=1.0)
    parser.add_argument("--swap-at", type=float)
    parser.add_argument("--missing", type=float, default=0.0)
    parser.add_argument("--delay-scale", type=float, default=1.0)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--out", type=Path, help="a directory for the JSONL files")
    where.add_argument("--url", help="a trimset server, e.g. http://localhost:8800")
    args = parser.parse_args(argv)

    now = datetime.now(UTC).replace(microsecond=0)
    end = args.end or now
    if end.tzinfo is None:
        parser.error("--end needs a time zone, e.g. 2026-11-11T00:00:00Z")
    try:
        plants = Plants(
            slope=args.slope,
            bias=args.bias,
            drift_at=args.drift_at,
            drift=args.drift,
            swap_at=args.swap_at,
            missing=args.missing,
            delay_scale=args.delay_scale,
        )
    except ValueError as e:
        parser.error(str(e))
    data = generate(
        args.scenario, seed=args.seed, end=end, days=args.days, per_day=args.per_day, plants=plants
    )

    if args.out:
        write_jsonl(data, args.out)
        print(json.dumps({"records": len(data.records), "outcomes": len(data.outcomes)}))
        return
    key = os.environ.get("TRIMSET_INGEST_KEY")
    if not key:
        parser.error("--url needs an ingest key in TRIMSET_INGEST_KEY")
    with httpx2.Client(base_url=args.url, timeout=60) as client:
        totals = send(data, client, key, admin_key=os.environ.get("TRIMSET_ADMIN_KEY"), now=now)
    json.dump(totals, sys.stdout)
    print()

"""The coverage experiment (T2.5, NFR-09, SM-2): do certified policies keep their promise?

For each seed and scenario: generate traffic (tools/datagen), fit a calibration map on
the fit split, compile a policy on the tune split, then measure the rule's true error
rate on fresh traffic from the same distribution. The truth is exact: the generator knows
each item's class probabilities q, so the error rate is the mean, over the items the rule
fires on, of the probability that its action is wrong. No labels are sampled for it.

NFR-09: on data that meets the assumptions (exchangeable traffic, correct labels), the
rate is within alpha in at least 95% of seeds for a 95% guarantee. A procedure that holds
in exactly 95% of seeds still lands below that share in about half of all runs, so the
test fails only when the share is credibly below 95%: an exact binomial test at 1%.
Seeds where the compiler declines are counted, not hidden.

Run nightly (.github/workflows/nightly.yml). Fewer seeds while developing:
    TRIMSET_COVERAGE_SEEDS=5 uv run pytest research -m slow
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scipy import stats

from trimset_stats import calibration, compiler
from trimset_stats.compiler import Actions, Compiled, Target
from trimset_stats.dataset import Item, Split, build
from trimset_tools.datagen import SCENARIOS, Plants, generate

pytestmark = [pytest.mark.slow, pytest.mark.req("NFR-09")]

SEEDS = int(os.environ.get("TRIMSET_COVERAGE_SEEDS", "200"))
END = datetime(2026, 11, 1, tzinfo=UTC)
WINDOW = {"from": "2026-10-02T00:00:00Z", "to": "2026-11-01T00:00:00Z"}


@dataclass(frozen=True)
class Case:
    scenario: str
    kind: compiler.QuestionType
    actions: Actions
    target: Target
    rule: str  # the certified rule whose rate is measured
    wrong: Callable[[np.ndarray, np.ndarray], np.ndarray]  # (q, top option) -> P(wrong)
    plants: Plants = field(default_factory=Plants)


def _fraud(q: np.ndarray, top: np.ndarray) -> np.ndarray:
    return q[:, 1]  # an accepted refund is wrong when it is fraud


def _not_top(q: np.ndarray, top: np.ndarray) -> np.ndarray:
    return 1 - q[np.arange(len(top)), top]  # a selected team is wrong unless it is the truth


def _hard(q: np.ndarray, top: np.ndarray) -> np.ndarray:
    return q[:, 2:].sum(axis=1)  # a request sent to the small model is wrong at level >= 2


CASES = [
    # A miscalibrated refund-fraud model: calibration first, then the guarantee.
    Case("refund-fraud", "noul", Actions(), Target("risk", "false_pass", alpha=0.02),
         "accept", _fraud, Plants(slope=0.5, bias=1.0)),
    Case("ticket-triage", "choice", Actions(),
         Target("risk", "error_budget", alpha=0.10), "select", _not_top),
    Case("model-routing", "score", Actions(accept_below_level=2),
         Target("risk", "false_pass", alpha=0.05), "accept", _hard),
]  # fmt: skip


def _items(
    scenario: str, seed: int, days: float, per_day: int, plants: Plants
) -> tuple[list[Item], np.ndarray]:
    """Generated decisions with their true labels, and each one's true class
    probabilities q."""
    data = generate(scenario, seed=seed, end=END, days=days, per_day=per_day, plants=plants)
    classes = SCENARIOS[scenario].classes
    items = []
    for record, truth in zip(data.records, data.truth, strict=True):
        p = record["probabilities"]
        probs = (1 - p[0], p[0]) if record["type"] == "noul" else tuple(p)
        items.append(
            Item(
                decision_id=record["decision_id"],
                ts=datetime.fromisoformat(record["ts"]).timestamp(),
                probs=probs,
                label=classes.index(truth["label"]),
            )
        )
    return items, np.array([t["q"] for t in data.truth])


def _split(items: list[Item]) -> Split:
    return Split(
        tuple(i.decision_id for i in items),
        np.array([i.probs for i in items]),
        np.array([i.label for i in items], dtype=np.intp),
        np.zeros(len(items), bool),
    )


def run(case: Case, seed: int) -> dict[str, Any]:
    items, _ = _items(case.scenario, seed, 30, 400, case.plants)
    data = build(items, now=END.timestamp(), seed=seed)
    fit = data.fit
    if case.kind == "noul":
        chosen = calibration.choose(fit.probs[:, 1], fit.labels, seed=seed).calibrator
    else:
        chosen = calibration.choose_multiclass(fit.probs, fit.labels, seed=seed).calibrator
    out = compiler.compile_policy(
        case.kind, chosen, data.tune, data.eval, case.actions, case.target, window=WINDOW
    )
    if not isinstance(out, Compiled):
        return {"seed": seed, "certified": False, "reason": out.reason}
    fresh, q = _items(case.scenario, 100_000 + seed, 50, 400, case.plants)
    scored = compiler.score(case.kind, chosen, _split(fresh), case.actions)
    action, by = compiler.decide(out.rules, scored, case.actions)
    rule = next(i for i, r in enumerate(out.rules) if r.id == case.rule)
    fired = by == rule
    rate = float(case.wrong(q, scored.top)[fired].mean()) if fired.any() else None
    return {
        "seed": seed,
        "certified": True,
        "method": chosen.method,
        "rate": rate,
        "holds": rate is None or rate <= case.target.alpha,  # type: ignore[operator]
        "automated": float((action != 0).mean()),
    }


@pytest.mark.parametrize("case", CASES, ids=[c.scenario for c in CASES])
def test_certified_policies_hold_on_fresh_traffic(case: Case) -> None:
    results = [run(case, seed) for seed in range(SEEDS)]
    certified = [r for r in results if r["certified"]]
    held = sum(r["holds"] for r in certified)
    summary = {
        "scenario": case.scenario,
        "alpha": case.target.alpha,
        "delta": case.target.delta,
        "seeds": SEEDS,
        "certified": len(certified),
        "held": held,
        "held_share": held / len(certified) if certified else None,
        "mean_rate": float(np.mean([r["rate"] for r in certified if r["rate"] is not None]))
        if certified
        else None,
        "mean_automated": float(np.mean([r["automated"] for r in certified]))
        if certified
        else None,
        "declined": {
            reason: sum(r.get("reason") == reason for r in results)
            for reason in ("not_enough_labels", "infeasible")
        },
    }
    summary["p_below_95"] = float(stats.binom.cdf(held, len(certified), 0.95)) if certified else 0.0
    if out := os.environ.get("TRIMSET_RESEARCH_OUT"):
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"coverage-{case.scenario}.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))
    assert len(certified) >= 0.9 * SEEDS, summary  # the experiment must mostly certify
    # NFR-09: the share that held isn't credibly below 95% (exact binomial, 1%).
    assert summary["p_below_95"] >= 0.01, summary

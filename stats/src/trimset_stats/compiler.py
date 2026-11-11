"""The policy compiler: from a target to thresholds, with a guarantee (FR-20 to FR-26,
ADR 0006).

A policy is at most two one-sided rules on calibrated scalars (FR-20), checked in order,
the first that fires deciding; nothing firing means ESCALATE:

| type   | accept rule                  | block or select rule              |
|--------|------------------------------|-----------------------------------|
| noul   | ACCEPT when s < lo           | BLOCK when s >= hi                |
|        | (high_means "accept": s >= hi, and BLOCK when s < lo)            |
| choice | -                            | SELECT:top when top >= t (BLOCK for block_options) |
| score  | ACCEPT when P(level >= a) < l | BLOCK when P(level >= b) >= h     |

**Guarantees (Learn-then-Test, Angelopoulos 2021; exact binomial, Clopper 1934).** A rule's
risk is P(wrong | it fires). Candidate thresholds are quantiles of the tune split's scalar,
walked from strictest to loosest; at each, n items fire and k of them are wrong, and the
p-value is P(Binomial(n, alpha) <= k). The walk keeps thresholds while p <= delta and stops
at the first that fails (fixed-sequence testing); the loosest kept is chosen.

The walk starts at the first threshold that fires on at least n_min = ceil(ln delta /
ln(1 - alpha)) items: with fewer, even no errors at all can't pass (Priye 2026), and
starting earlier would stop the walk at once. The cut uses only the scalars, never the
labels.

- An item that fires without an outcome counts as wrong: that can only make a bound more
  cautious. On a path set as `audited_only`, only audited items count, a random sample
  (Lakkaraju 2017).
- **false_pass** certifies the accept rule. The block rule, with an escalation cap, gets
  the strictest threshold that keeps escalation within it: it blocks as little as the cap
  allows, and its error rate is reported, not guaranteed.
- **error_budget** certifies every rule at alpha, delta split equally (Bonferroni). A rule
  that can't be certified is left out, so its items escalate.
- **cost** minimises the expected cost on the tune split (Elkan 2001), with no guarantee.

Metrics on both splits follow FR-22; `simulate` gives them for any thresholds (FR-26).
"""

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
from scipy import stats

from trimset_stats.calibration import Calibrator
from trimset_stats.dataset import Split
from trimset_stats.metrics import clopper_pearson

QuestionType = Literal["noul", "choice", "score"]
Action = Literal["ACCEPT", "BLOCK", "SELECT"]
Scalar = str | int  # "p", "top", or k for P(level >= k)
GRID = 200
COST_GRID = 100  # per rule when two rules search together
METHOD = "ltt-binomial/1"
_ESCALATE, _ACCEPT, _BLOCK, _SELECT = 0, 1, 2, 3
_CODES: dict[str, int] = {"ACCEPT": _ACCEPT, "BLOCK": _BLOCK, "SELECT": _SELECT}
Bools = npt.NDArray[np.bool_]
Floats = npt.NDArray[np.float64]


@dataclass(frozen=True)
class Actions:
    """How probabilities become actions (FR-20, QuestionSettings.actions)."""

    high_means: Literal["block", "accept"] = "block"  # noul
    block_options: tuple[int, ...] = ()  # choice: options that BLOCK instead of SELECT
    accept_below_level: int | None = None  # score
    block_from_level: int | None = None  # score


@dataclass(frozen=True)
class Costs:
    escalation: float
    wrong: Mapping[str, float]  # by action: ACCEPT, BLOCK, SELECT


@dataclass(frozen=True)
class Target:
    """FR-21: a risk target (false_pass or error_budget, alpha at confidence 1 - delta) or
    a cost model, either with an optional escalation cap."""

    kind: Literal["risk", "cost"]
    metric: Literal["false_pass", "error_budget"] | None = None
    alpha: float | None = None
    delta: float = 0.05
    max_escalation: float | None = None
    costs: Costs | None = None

    def __post_init__(self) -> None:
        if self.kind == "risk" and (
            self.metric is None or self.alpha is None or not 0 < self.alpha < 1
        ):
            raise ValueError("a risk target needs a metric and alpha in (0, 1)")
        if not 0 < self.delta < 1:
            raise ValueError("delta must be in (0, 1)")
        if self.kind == "cost" and self.costs is None:
            raise ValueError("a cost target needs costs")
        if self.max_escalation is not None and not 0 <= self.max_escalation <= 1:
            raise ValueError("max_escalation must be in [0, 1]")

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.kind == "risk":
            out |= {"metric": self.metric, "alpha": self.alpha, "delta": self.delta}
        if self.max_escalation is not None:
            out["max_escalation"] = self.max_escalation
        if self.costs is not None:
            out["costs"] = {"escalation": self.costs.escalation, "wrong": dict(self.costs.wrong)}
        return out


@dataclass(frozen=True)
class Rule:
    id: Literal["accept", "block", "select"]
    action: Action
    scalar: Scalar
    op: Literal["lt", "ge"]
    threshold: float

    def to_json(self) -> dict[str, Any]:
        scalar: Any = self.scalar if isinstance(self.scalar, str) else {"tail_ge": self.scalar}
        return {
            "id": self.id,
            "action": self.action,
            "scalar": scalar,
            "op": self.op,
            "threshold": self.threshold,
        }

    def strict_to_loose(self, candidates: Floats) -> Floats:
        """`lt` fires on more as the threshold rises, `ge` as it falls."""
        return np.sort(candidates) if self.op == "lt" else np.sort(candidates)[::-1]


# ------------------------------------------------------------------ scoring a split


@dataclass(frozen=True)
class Scored:
    """What the rules read on one split: each item's scalars, which actions would be
    right for it, and whether it has an outcome and was audited."""

    values: dict[Scalar, Floats]
    right: dict[str, Bools]
    top: npt.NDArray[np.intp]
    labeled: Bools
    audit: Bools

    @property
    def n(self) -> int:
        return int(self.labeled.size)


def score(kind: QuestionType, calibrator: Calibrator, split: Split, actions: Actions) -> Scored:
    labels, labeled = split.labels, split.labeled
    n = labels.size
    if kind == "noul":
        s = calibrator.apply(split.probs[:, 1]) if n else np.empty(0)
        yes = labels == 1
        accept_right = ~yes if actions.high_means == "block" else yes
        right = {"ACCEPT": accept_right & labeled, "BLOCK": ~accept_right & labeled}
        return Scored({"p": s}, right, np.zeros(n, np.intp), labeled, split.audit)
    dist = calibrator.apply(split.probs) if n else np.empty((0, split.probs.shape[1]))
    top = dist.argmax(axis=1).astype(np.intp)
    if kind == "choice":
        hit = (labels == top) & labeled
        return Scored(
            {"top": dist.max(axis=1)}, {"SELECT": hit, "BLOCK": hit}, top, labeled, split.audit
        )
    values: dict[Scalar, Floats] = {}
    correct: dict[str, Bools] = {}
    if actions.accept_below_level is not None:
        a = actions.accept_below_level
        values[a] = dist[:, a:].sum(axis=1)
        correct["ACCEPT"] = (labels < a) & labeled
    if actions.block_from_level is not None:
        b = actions.block_from_level
        values[b] = dist[:, b:].sum(axis=1)
        correct["BLOCK"] = (labels >= b) & labeled
    return Scored(values, correct, top, labeled, split.audit)


def _fires(rule: Rule, scored: Scored) -> Bools:
    v = scored.values[rule.scalar]
    return np.asarray(v < rule.threshold if rule.op == "lt" else v >= rule.threshold)


def decide(
    rules: Sequence[Rule], scored: Scored, actions: Actions
) -> tuple[npt.NDArray[np.int8], npt.NDArray[np.int8]]:
    """Each item's action code and the index of the rule that decided it (-1: none)."""
    action = np.full(scored.n, _ESCALATE, np.int8)
    by = np.full(scored.n, -1, np.int8)
    for i, rule in enumerate(rules):
        hit = _fires(rule, scored) & (by < 0)
        code = _CODES[rule.action]
        if rule.action == "SELECT" and actions.block_options:
            blocked = np.isin(scored.top, actions.block_options)
            action[hit & blocked], action[hit & ~blocked] = _BLOCK, _SELECT
        else:
            action[hit] = code
        by[hit] = i
    return action, by


def _wrong(action: npt.NDArray[np.int8], scored: Scored) -> Bools:
    """Labeled items whose action was wrong."""
    wrong = np.zeros(scored.n, np.bool_)
    for name, code in _CODES.items():
        if name in scored.right:
            here = action == code
            wrong |= here & scored.labeled & ~scored.right[name]
    return wrong


def simulate(
    rules: Sequence[Rule], scored: Scored, actions: Actions, costs: Costs | None = None
) -> dict[str, Any]:
    """FR-22 metrics for `rules` on a split: the shares automated and escalated, and for
    each rule what fired, what was wrong, what had no outcome, the error rate among
    labeled items with its exact 95% interval, and the expected cost (FR-26)."""
    action, by = decide(rules, scored, actions)
    wrong = _wrong(action, scored)
    n = scored.n
    out_rules: dict[str, Any] = {}
    for i, rule in enumerate(rules):
        fired = by == i
        f, w, u = int(fired.sum()), int((fired & wrong).sum()), int((fired & ~scored.labeled).sum())
        known = f - u
        out_rules[rule.id] = {
            "fired": f,
            "wrong": w,
            "unlabeled": u,
            "rate": w / known if known else None,
            "ci95": list(clopper_pearson(w, known)) if known else None,
        }
    escalated = int((action == _ESCALATE).sum())
    metrics: dict[str, Any] = {
        "n": n,
        "automated": (n - escalated) / n if n else 0.0,
        "escalated": escalated / n if n else 0.0,
        "rules": out_rules,
    }
    metrics["expected_cost_per_1000"] = (
        None if costs is None or not n else 1000 * _cost(action, wrong, scored, costs) / n
    )
    return metrics


def _cost(action: npt.NDArray[np.int8], wrong: Bools, scored: Scored, costs: Costs) -> float:
    """Escalations, and wrong actions; a fired item without an outcome counts as wrong."""
    total = costs.escalation * float((action == _ESCALATE).sum())
    for name, code in _CODES.items():
        bad = (action == code) & (wrong | ~scored.labeled)
        total += costs.wrong.get(name, 0.0) * float(bad.sum())
    return total


# ------------------------------------------------------------------ certification


def n_min(alpha: float, delta: float) -> int:
    """The fewest error-free items that certify P(wrong) <= alpha at confidence 1 - delta:
    598 for alpha = 0.005, delta = 0.05 (Priye 2026)."""
    return math.ceil(math.log(delta) / math.log1p(-alpha))


def _candidates(values: Floats, rule: Rule) -> Floats:
    """Up to GRID + 1 quantiles of the scalar, plus the end that fires on everything."""
    grid = np.quantile(values, np.linspace(0, 1, GRID + 1)) if values.size else np.empty(0)
    end = np.array([1.0 if rule.op == "lt" else 0.0])
    return rule.strict_to_loose(np.unique(np.concatenate([grid, end]).clip(0, 1)))


@dataclass(frozen=True)
class Certified:
    rule: Rule | None  # None when no threshold passed
    n: int = 0  # items the chosen threshold was certified on
    most: int = 0  # the most items any threshold had to test on
    tested: bool = False  # some threshold had enough items to test
    best_alpha: float | None = None  # the smallest bound any tested threshold reached


def certify(
    template: Rule,
    scored: Scored,
    alpha: float,
    delta: float,
    *,
    undecided: Bools,
    audited_only: bool = False,
) -> Certified:
    """The loosest threshold for `template` whose risk is certified <= alpha (see the
    module doc). `undecided` marks items earlier rules left alone."""
    values = scored.values[template.scalar]
    eligible = undecided & (scored.audit if audited_only else np.ones(scored.n, np.bool_))
    right = scored.right[template.action if template.action != "SELECT" else "SELECT"]
    bad = ~right  # wrong, or no outcome
    need = n_min(alpha, delta)
    chosen: Rule | None = None
    chosen_n, most, tested, best = 0, 0, False, None
    for threshold in _candidates(values, template):
        fired = (values < threshold if template.op == "lt" else values >= threshold) & eligible
        n = int(fired.sum())
        most = max(most, n)
        if n < need:
            continue  # the walk starts here; n only grows as thresholds loosen
        tested = True
        k = int((fired & bad).sum())
        upper = float(stats.beta.ppf(1 - delta, k + 1, n - k)) if k < n else 1.0
        best = upper if best is None else min(best, upper)
        if stats.binom.cdf(k, n, alpha) > delta:
            break
        chosen = Rule(template.id, template.action, template.scalar, template.op, float(threshold))
        chosen_n = n
    return Certified(chosen, chosen_n, most, tested, best)


# ------------------------------------------------------------------ compiling


@dataclass(frozen=True)
class Compiled:
    rules: list[Rule]
    guarantee: dict[str, Any]
    metrics: dict[str, Any]  # {"tune": ..., "eval": ...}
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Declined:
    """No policy (FR-23, FR-24): too few labels, or a target that can't be met."""

    reason: Literal["not_enough_labels", "infeasible"]
    message: str
    needed: int | None = None  # more labeled items in all
    binding: str | None = None  # "alpha" or "max_escalation"
    best: dict[str, float] = field(default_factory=dict)


def templates(kind: QuestionType, actions: Actions) -> list[Rule]:
    """The rules a question can have, thresholds still unset."""
    if kind == "noul":
        block_high = actions.high_means == "block"
        return [
            Rule("accept", "ACCEPT", "p", "lt" if block_high else "ge", 0.0),
            Rule("block", "BLOCK", "p", "ge" if block_high else "lt", 0.0),
        ]
    if kind == "choice":
        return [Rule("select", "SELECT", "top", "ge", 0.0)]
    out = []
    if actions.accept_below_level is not None:
        out.append(Rule("accept", "ACCEPT", actions.accept_below_level, "lt", 0.0))
    if actions.block_from_level is not None:
        out.append(Rule("block", "BLOCK", actions.block_from_level, "ge", 0.0))
    if not out:
        raise ValueError("a score question needs accept_below_level or block_from_level")
    return out


def _shortfall(certified: Certified, alpha: float, delta: float) -> Declined:
    if not certified.tested:
        more = n_min(alpha, delta) - certified.most
        return Declined(
            "not_enough_labels",
            f"Certifying {alpha:g} at {1 - delta:.0%} needs {n_min(alpha, delta)} labeled items "
            f"where the rule fires in the tuning split, and there are {certified.most}: "
            f"about {math.ceil(more / 0.4)} more labeled decisions in all.",
            needed=math.ceil(more / 0.4),
        )
    best = {"alpha": round(certified.best_alpha or 1.0, 6)}
    return Declined(
        "infeasible",
        f"Even the strictest threshold makes too many mistakes to certify {alpha:g} at "
        f"{1 - delta:.0%}; the best bound reached is {best['alpha']:g}.",
        binding="alpha",
        best=best,
    )


def _escalation(rules: Sequence[Rule], scored: Scored, actions: Actions) -> float:
    action, _ = decide(rules, scored, actions)
    return float((action == _ESCALATE).mean()) if scored.n else 0.0


def compile_policy(
    kind: QuestionType,
    calibrator: Calibrator,
    tune: Split,
    evaluation: Split,
    actions: Actions,
    target: Target,
    *,
    window: Mapping[str, str],
    audited_only: Collection[str] = (),
) -> Compiled | Declined:
    """The policy for one group, or why there can't be one."""
    t_scored = score(kind, calibrator, tune, actions)
    e_scored = score(kind, calibrator, evaluation, actions)
    shapes = templates(kind, actions)
    if target.kind == "cost":
        return _compile_cost(shapes, t_scored, e_scored, actions, target)

    alpha, delta = _alpha(target), target.delta
    notes: list[str] = []
    undecided = np.ones(t_scored.n, np.bool_)
    rules: list[Rule] = []
    certified_n: dict[str, int] = {}

    if target.metric == "false_pass":
        accept = next((r for r in shapes if r.action == "ACCEPT"), None)
        if accept is None:
            raise ValueError(
                "a false-pass target needs an ACCEPT rule; choice questions use error_budget"
            )
        result = certify(accept, t_scored, alpha, delta, undecided=undecided,
                         audited_only="ACCEPT" in audited_only)  # fmt: skip
        if result.rule is None:
            return _shortfall(result, alpha, delta)
        rules.append(result.rule)
        certified_n[result.rule.id] = result.n
        block = next((r for r in shapes if r.action == "BLOCK"), None)
        if target.max_escalation is not None:
            if _escalation(rules, t_scored, actions) > target.max_escalation and block is None:
                return _cap_binds(rules, t_scored, actions, target.max_escalation)
            if _escalation(rules, t_scored, actions) > target.max_escalation and block is not None:
                chosen = _least_blocking(rules, block, t_scored, actions, target.max_escalation)
                if chosen is None:
                    return _cap_binds(
                        [*rules, _all(block)], t_scored, actions, target.max_escalation
                    )
                rules.append(chosen)
                notes.append("The block rule's error rate is reported, not guaranteed.")
    else:
        share = delta / len(shapes)  # Bonferroni across the rules
        for shape in shapes:
            result = certify(shape, t_scored, alpha, share, undecided=undecided,
                             audited_only=shape.action in audited_only)  # fmt: skip
            if result.rule is None:
                if len(shapes) > 1:
                    notes.append(f"No {shape.id} threshold could be certified; its items escalate.")
                    continue
                return _shortfall(result, alpha, share)
            rules.append(result.rule)
            certified_n[result.rule.id] = result.n
            undecided &= ~_fires(result.rule, t_scored)
        if not rules:
            return _shortfall(result, alpha, share)
        if (
            target.max_escalation is not None
            and _escalation(rules, t_scored, actions) > target.max_escalation
        ):
            return _cap_binds(rules, t_scored, actions, target.max_escalation)

    costs = target.costs
    return Compiled(
        rules=rules,
        guarantee=_guarantee(target, rules, certified_n, window),
        metrics={
            "tune": simulate(rules, t_scored, actions, costs),
            "eval": simulate(rules, e_scored, actions, costs),
        },
        notes=notes,
    )


def _alpha(target: Target) -> float:
    if target.alpha is None:  # pragma: no cover - Target refuses a risk target without alpha
        raise ValueError("a risk target needs alpha")
    return target.alpha


def _all(rule: Rule) -> Rule:
    """The rule at the threshold that fires on everything."""
    return Rule(rule.id, rule.action, rule.scalar, rule.op, 1.0 if rule.op == "lt" else 0.0)


def _least_blocking(
    rules: list[Rule], block: Rule, scored: Scored, actions: Actions, cap: float
) -> Rule | None:
    """The strictest block threshold that brings escalation within the cap."""
    for threshold in _candidates(scored.values[block.scalar], block):
        candidate = Rule(block.id, block.action, block.scalar, block.op, float(threshold))
        if _escalation([*rules, candidate], scored, actions) <= cap:
            return candidate
    return None


def _cap_binds(rules: Sequence[Rule], scored: Scored, actions: Actions, cap: float) -> Declined:
    best = round(_escalation(rules, scored, actions), 6)
    return Declined(
        "infeasible",
        f"Meeting the target escalates {best:.1%} of decisions, over the {cap:.1%} cap.",
        binding="max_escalation",
        best={"escalated": best},
    )


def _guarantee(
    target: Target, rules: Sequence[Rule], n: Mapping[str, int], window: Mapping[str, str]
) -> dict[str, Any]:
    alpha, delta = _alpha(target), target.delta
    held = [r for r in rules if r.id in n]
    which = " and ".join(
        f"P(wrong | {r.action}{':x' if r.action == 'SELECT' else ''})" for r in held
    )
    return {
        "status": "certified",
        "method": METHOD,
        "statement": (
            f"With probability at least {1 - delta:.0%} over the tuning data, {which} "
            f"{'is' if len(held) == 1 else 'are each'} at most {alpha:.2%} on future traffic "
            "that is exchangeable with it."
        ),
        "alpha": alpha,
        "delta": delta,
        "assumptions": [
            "Future traffic is exchangeable with the tuning window.",
            "The outcomes in the tuning split are correct.",
            "A decision that fired without an outcome counts as wrong.",
        ],
        "n": dict(n),
        "window": dict(window),
    }


def _compile_cost(
    shapes: list[Rule], tune: Scored, evaluation: Scored, actions: Actions, target: Target
) -> Compiled | Declined:
    """The thresholds with the lowest expected cost on the tune split, within the cap."""
    costs = target.costs
    if costs is None:  # pragma: no cover - Target refuses a cost target without costs
        raise ValueError("a cost target needs costs")
    grids = []
    for shape in shapes:
        values = tune.values[shape.scalar]
        size = GRID if len(shapes) == 1 else COST_GRID
        points = np.quantile(values, np.linspace(0, 1, size + 1)) if values.size else np.empty(0)
        grids.append(np.unique(np.concatenate([points, [0.0, 1.0]]).clip(0, 1)))
    best: tuple[float, list[Rule]] | None = None
    lowest_escalation = 1.0
    for combo in np.array(np.meshgrid(*grids, indexing="ij")).reshape(len(shapes), -1).T:
        rules = [
            Rule(s.id, s.action, s.scalar, s.op, float(t))
            for s, t in zip(shapes, combo, strict=True)
        ]
        action, _ = decide(rules, tune, actions)
        escalated = float((action == _ESCALATE).mean()) if tune.n else 0.0
        lowest_escalation = min(lowest_escalation, escalated)
        if target.max_escalation is not None and escalated > target.max_escalation:
            continue
        cost = _cost(action, _wrong(action, tune), tune, costs)
        if best is None or cost < best[0]:
            best = (cost, rules)
    # Each grid holds a threshold that fires on everything, so escalation 0 is always on
    # offer and some combination always meets the cap.
    if best is None:  # pragma: no cover
        raise ValueError(f"no thresholds keep escalation within the cap ({lowest_escalation:.1%})")
    rules = best[1]
    return Compiled(
        rules=rules,
        guarantee={
            "status": "none",
            "reason": "cost_target",
            "detail": "Expected-cost optimum on the tuning split.",
        },
        metrics={
            "tune": simulate(rules, tune, actions, costs),
            "eval": simulate(rules, evaluation, actions, costs),
        },
    )

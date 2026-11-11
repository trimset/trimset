"""Applying a policy (sdk.md §3, steps 4 to 8): the algorithm every SDK implements exactly,
in float64. Pure Python, mirroring trimset-stats' numpy operations one for one, so gate()
needs no numpy and agrees with what the compiler certified.
"""

import hashlib
import json
import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

CLAMP = 1e-6


@dataclass(frozen=True)
class Applied:
    action: str  # ACCEPT, BLOCK, ESCALATE or SELECT:<value>
    label: Any  # the selected option's value, for SELECT
    scalar: float  # what the deciding rule read (the first rule's, when none fired)


def audited(decision_id: str, audit_fraction: float) -> bool:
    """FR-37: a decision is audited when its id's hash falls under the fraction."""
    if audit_fraction <= 0:
        return False
    u = int(hashlib.sha256(decision_id.lower().encode()).hexdigest()[:16], 16) / 2**64
    return u < audit_fraction


def _clamp(p: float) -> float:
    return min(max(p, CLAMP), 1 - CLAMP)  # np.clip


def _logit(q: float) -> float:
    return math.log(q / (1 - q))


def _sigmoid(z: float) -> float:
    # exp(-logaddexp(0, -z)), as numpy computes logaddexp
    lae = math.log1p(math.exp(-z)) if z > 0 else -z + math.log1p(math.exp(z))
    return math.exp(-lae)


def _interp(x: float, xp: Sequence[float], fp: Sequence[float]) -> float:
    """numpy.interp for one point: linear between breakpoints, the end values outside."""
    if x >= xp[-1]:
        return float(fp[-1])
    if x < xp[0]:
        return float(fp[0])
    j = bisect_right(xp, x) - 1
    if xp[j] == x:
        return float(fp[j])
    slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j])
    return float(slope * (x - xp[j]) + fp[j])


def calibrate_event(calibration: Mapping[str, Any], p: float) -> float:
    """A noul question's calibrated probability (step 5)."""
    q, method = _clamp(p), calibration["method"]
    if method == "identity":
        return q
    if method == "temperature":
        return _sigmoid(_logit(q) / calibration["temperature"])
    if method == "platt":
        return _sigmoid(-(calibration["a"] * _logit(q) + calibration["b"]))
    if method == "isotonic":
        return _interp(q, calibration["x"], calibration["y"])
    raise ValueError(f"unknown calibration method {method!r}")


def calibrate_distribution(calibration: Mapping[str, Any], probs: Sequence[float]) -> list[float]:
    """A choice or score question's calibrated distribution (step 5)."""
    qs, method = [_clamp(p) for p in probs], calibration["method"]
    if method == "identity":
        return qs
    if method == "temperature":
        z = [math.log(q) / calibration["temperature"] for q in qs]
        top = max(z)
        weights = [math.exp(v - top) for v in z]
        total = sum(weights)
        return [w / total for w in weights]
    raise ValueError(f"{method} calibrates one event's probability, not a distribution")


def _same(a: Any, b: Any) -> bool:
    """JSON equality: true and "true" are different options."""
    return type(a) is type(b) and a == b


def select_action(value: Any) -> str:
    """SELECT:<value>, with a boolean written as JSON writes it."""
    return "SELECT:" + (value if isinstance(value, str) else json.dumps(value))


def apply(
    policy: Mapping[str, Any], definition: Mapping[str, Any], probs: Sequence[float]
) -> Applied:
    """Steps 5 to 8. `probs` is [p] for noul, otherwise one probability per option or
    level, summing to 1."""
    calibration, kind = policy["calibration"], policy["type"]
    top = 0
    if kind == "noul":
        dist = [calibrate_event(calibration, probs[0])]
    else:
        dist = calibrate_distribution(calibration, probs)
        top = dist.index(max(dist))  # the first option holding the maximum
    values = [o["value"] for o in definition.get("options") or []]

    def scalar(rule: Mapping[str, Any]) -> float:
        name = rule["scalar"]
        if name == "p":
            return dist[0]
        if name == "top":
            return dist[top]
        # Left to right, as numpy sums fewer than 8 values; more levels may differ by 1 ulp.
        return sum(dist[name["tail_ge"] :])

    rules = policy["rules"]
    for rule in rules:
        value = scalar(rule)
        threshold = rule["threshold"]
        if rule["action"] == "SELECT":
            for option in rule.get("per_option") or []:
                if _same(option["value"], values[top]):
                    threshold = option["threshold"]
        if value < threshold if rule["op"] == "lt" else value >= threshold:
            if rule["action"] != "SELECT":
                return Applied(rule["action"], None, value)
            chosen = values[top]
            if any(_same(chosen, v) for v in policy.get("option_actions") or []):
                return Applied("BLOCK", chosen, value)
            return Applied(select_action(chosen), chosen, value)
    return Applied(policy["fallback_action"], None, scalar(rules[0]) if rules else dist[top])

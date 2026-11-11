"""The SDK's gate() and the compiler decide alike (sdk.md §3).

The SDK applies a policy in pure Python; the compiler certified it with numpy. On random
calibrations, rules and answers rounded to 4 decimals, as vendors send them, every
decision and every scalar must agree.
"""

import numpy as np
import pytest

from trimset import policy as sdk
from trimset_stats import compiler
from trimset_stats.calibration import Calibrator
from trimset_stats.compiler import Actions, Rule
from trimset_stats.dataset import Split

N = 3000
NAMES = {0: "ESCALATE", 1: "ACCEPT", 2: "BLOCK", 3: "SELECT"}


def _split(probs: np.ndarray) -> Split:
    n = len(probs)
    return Split(tuple(map(str, range(n))), probs, np.zeros(n, np.intp), np.zeros(n, bool))


def _check(kind: str, cal: Calibrator, probs: np.ndarray, rules: list[Rule], actions: Actions,
           definition: dict, option_actions: list | None = None) -> None:  # fmt: skip
    scored = compiler.score(kind, cal, _split(probs), actions)
    codes, by = compiler.decide(rules, scored, actions)
    policy = {
        "type": kind, "calibration": cal.to_json(), "rules": [r.to_json() for r in rules],
        "fallback_action": "ESCALATE", "option_actions": option_actions or [],
    }  # fmt: skip
    values = [o["value"] for o in definition.get("options", [])]
    for i in range(len(probs)):
        given = [probs[i, 1]] if kind == "noul" else list(probs[i])
        out = sdk.apply(policy, definition, given)
        assert out.action.split(":")[0] == NAMES[int(codes[i])], (i, given, out)
        rule = rules[by[i]] if by[i] >= 0 else rules[0]
        assert out.scalar == pytest.approx(scored.values[rule.scalar][i], abs=1e-12)
        if out.action.startswith("SELECT"):
            assert out.label == values[scored.top[i]]


@pytest.mark.parametrize(
    "cal",
    [
        Calibrator("identity"),
        Calibrator("temperature", temperature=1.7),
        Calibrator("platt", a=-0.71, b=0.42),
        Calibrator(
            "isotonic", x=(0.0, 0.05, 0.2, 0.5, 0.9, 1.0), y=(0.0, 0.01, 0.1, 0.4, 0.95, 1.0)
        ),
    ],
    ids=lambda c: c.method,
)
@pytest.mark.req("FR-33")
def test_noul(cal: Calibrator) -> None:
    rng = np.random.default_rng(1)
    p = np.round(rng.random(N), 4)
    probs = np.column_stack([1 - p, p])
    rules = [Rule("accept", "ACCEPT", "p", "lt", 0.2731), Rule("block", "BLOCK", "p", "ge", 0.8123)]
    definition = {"type": "noul"}
    _check("noul", cal, probs, rules, Actions(), definition)


@pytest.mark.parametrize(
    "cal", [Calibrator("identity"), Calibrator("temperature", temperature=0.6)]
)
def test_choice_with_a_blocked_option(cal: Calibrator) -> None:
    rng = np.random.default_rng(2)
    raw = np.round(rng.dirichlet([0.5] * 4, N), 4)
    probs = raw / raw.sum(axis=1, keepdims=True)  # as gate() normalizes
    definition = {"type": "choice", "options": [{"value": v} for v in ("a", "b", True, "d")]}
    rules = [Rule("select", "SELECT", "top", "ge", 0.6137)]
    _check("choice", cal, probs, rules, Actions(block_options=(1,)), definition, ["b"])


@pytest.mark.parametrize(
    "cal", [Calibrator("identity"), Calibrator("temperature", temperature=1.3)]
)
def test_score_tails(cal: Calibrator) -> None:
    rng = np.random.default_rng(3)
    raw = np.round(rng.dirichlet([1.0] * 5, N), 4)
    probs = raw / raw.sum(axis=1, keepdims=True)
    rules = [
        Rule("accept", "ACCEPT", 2, "lt", 0.2417),
        Rule("block", "BLOCK", 3, "ge", 0.5309),
    ]
    actions = Actions(accept_below_level=2, block_from_level=3)
    _check("score", cal, probs, rules, actions, {"type": "score"})

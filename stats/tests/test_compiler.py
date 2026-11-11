import json

import numpy as np
import pytest

from trimset_stats import compiler
from trimset_stats.calibration import Calibrator
from trimset_stats.compiler import Actions, Compiled, Costs, Declined, Rule, Target
from trimset_stats.dataset import NO_LABEL, Split

IDENTITY = Calibrator("identity")
WINDOW = {"from": "2026-08-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"}
BLOCK_HIGH = Actions()


def _noul(seed: int, n: int, base: float = 0.06, sharp: float = 0.25) -> Split:
    """A calibrated noul model: q drawn around `base`, the outcome drawn from q."""
    rng = np.random.default_rng(seed)
    a = base / sharp
    q = rng.beta(a, (1 - base) / sharp, size=n)
    y = (rng.random(n) < q).astype(np.intp)
    return Split(
        tuple(f"{seed}-{i}" for i in range(n)), np.column_stack([1 - q, q]), y, np.zeros(n, bool)
    )


def _multi(seed: int, n: int, k: int, sharpness: float = 0.4) -> Split:
    rng = np.random.default_rng(seed)
    dist = rng.dirichlet(np.full(k, sharpness), size=n)
    labels = np.array([rng.choice(k, p=row) for row in dist], dtype=np.intp)
    return Split(tuple(f"{seed}-{i}" for i in range(n)), dist, labels, np.zeros(n, bool))


def _compile(
    split: Split, target: Target, actions: Actions = BLOCK_HIGH, kind: str = "noul", **kw: object
) -> Compiled | Declined:
    return compiler.compile_policy(
        kind, IDENTITY, split, split, actions, target, window=WINDOW, **kw
    )  # type: ignore[arg-type]


def test_how_many_error_free_items_a_target_needs() -> None:
    assert compiler.n_min(0.005, 0.05) == 598  # the requirements' worked example (FR-23)
    assert compiler.n_min(0.02, 0.05) == 149


@pytest.mark.req("FR-22")
def test_simulate_counts_by_hand() -> None:
    probs = np.array([[0.9, 0.1], [0.8, 0.2], [0.5, 0.5], [0.1, 0.9], [0.05, 0.95], [0.7, 0.3]])
    labels = np.array([0, 1, 0, 1, 0, NO_LABEL], dtype=np.intp)
    split = Split(tuple("abcdef"), probs, labels, np.zeros(6, bool))
    scored = compiler.score("noul", IDENTITY, split, BLOCK_HIGH)
    rules = [Rule("accept", "ACCEPT", "p", "lt", 0.35), Rule("block", "BLOCK", "p", "ge", 0.9)]
    m = compiler.simulate(rules, scored, BLOCK_HIGH, Costs(1.0, {"ACCEPT": 10.0, "BLOCK": 2.0}))
    assert (m["n"], m["automated"], m["escalated"]) == (6, 5 / 6, 1 / 6)
    accept = m["rules"]["accept"]
    assert (accept["fired"], accept["wrong"], accept["unlabeled"], accept["rate"]) == (3, 1, 1, 0.5)
    assert accept["ci95"] == pytest.approx(list(compiler.clopper_pearson(1, 2)))
    assert m["rules"]["block"]["fired"] == 2 and m["rules"]["block"]["wrong"] == 1
    # One escalation, two wrong accepts (one of them unlabeled), one wrong block.
    assert m["expected_cost_per_1000"] == pytest.approx(1000 * (1 + 10 * 2 + 2 * 1) / 6)


@pytest.mark.req("FR-21")
@pytest.mark.req("FR-25")
def test_a_false_pass_target_certifies_the_accept_rule() -> None:
    tune = _noul(1, 6000)
    target = Target("risk", "false_pass", alpha=0.02, delta=0.05, max_escalation=0.15)
    out = _compile(tune, target)
    assert isinstance(out, Compiled), out
    accept = out.rules[0]
    assert (accept.id, accept.op) == ("accept", "lt")
    g = out.guarantee
    assert (g["status"], g["method"], g["alpha"], g["delta"]) == (
        "certified",
        "ltt-binomial/1",
        0.02,
        0.05,
    )
    assert g["n"]["accept"] >= compiler.n_min(0.02, 0.05) and "accept" in g["n"]
    assert g["window"] == WINDOW
    assert "P(wrong | ACCEPT)" in g["statement"] and "95%" in g["statement"]
    tune_rate = out.metrics["tune"]["rules"]["accept"]["rate"]
    assert tune_rate <= 0.02
    assert out.metrics["tune"]["escalated"] <= 0.15
    json.dumps([r.to_json() for r in out.rules] + [out.guarantee, out.metrics])  # plain JSON


@pytest.mark.req("FR-21")
def test_without_a_cap_a_false_pass_target_has_no_block_rule() -> None:
    out = _compile(_noul(2, 6000), Target("risk", "false_pass", alpha=0.02))
    assert isinstance(out, Compiled) and [r.id for r in out.rules] == ["accept"]


@pytest.mark.req("FR-23")
def test_too_few_labels_say_how_many_more_are_needed() -> None:
    out = _compile(_noul(3, 400), Target("risk", "false_pass", alpha=0.005))
    assert isinstance(out, Declined) and out.reason == "not_enough_labels"
    assert out.needed == pytest.approx((598 - 400) / 0.4, abs=1)
    assert "598" in out.message


@pytest.mark.req("FR-24")
def test_a_model_that_ranks_nothing_cant_meet_a_small_alpha() -> None:
    rng = np.random.default_rng(4)
    p = rng.random(5000)
    y = (rng.random(5000) < 0.3).astype(np.intp)  # outcomes ignore p
    split = Split(
        tuple(map(str, range(5000))), np.column_stack([1 - p, p]), y, np.zeros(5000, bool)
    )
    out = _compile(split, Target("risk", "false_pass", alpha=0.01))
    assert isinstance(out, Declined)
    assert (out.reason, out.binding) == ("infeasible", "alpha")
    assert out.best["alpha"] > 0.2


@pytest.mark.req("FR-24")
def test_an_escalation_cap_that_cant_be_met_names_itself() -> None:
    target = Target("risk", "error_budget", alpha=0.02, max_escalation=0.0)
    out = _compile(_noul(5, 6000), target)
    assert isinstance(out, Declined)
    assert (out.reason, out.binding) == ("infeasible", "max_escalation")
    assert 0 < out.best["escalated"] <= 1


@pytest.mark.req("FR-21")
def test_an_error_budget_holds_both_rules_and_splits_delta() -> None:
    out = _compile(_noul(6, 8000, base=0.4, sharp=1.0), Target("risk", "error_budget", alpha=0.05))
    assert isinstance(out, Compiled)
    assert [r.id for r in out.rules] == ["accept", "block"]
    for rule in ("accept", "block"):
        assert out.metrics["tune"]["rules"][rule]["rate"] <= 0.05
    assert set(out.guarantee["n"]) == {"accept", "block"}
    assert "are each at most 5.00%" in out.guarantee["statement"]


@pytest.mark.req("FR-20")
def test_when_high_means_accept_the_rules_flip() -> None:
    """UC-5: p is the probability the tool call is safe."""
    unsafe = _noul(7, 8000, base=0.03)
    safe_first = Split(unsafe.ids, unsafe.probs[:, ::-1].copy(), 1 - unsafe.labels, unsafe.audit)
    out = _compile(
        safe_first, Target("risk", "false_pass", alpha=0.02), Actions(high_means="accept")
    )
    assert isinstance(out, Compiled)
    assert (out.rules[0].action, out.rules[0].op) == ("ACCEPT", "ge")
    assert out.metrics["tune"]["rules"]["accept"]["rate"] <= 0.02


@pytest.mark.req("FR-20")
def test_a_choice_question_gets_a_certified_select_rule() -> None:
    split = _multi(8, 6000, 4, sharpness=0.3)
    out = _compile(split, Target("risk", "error_budget", alpha=0.05), kind="choice")
    assert isinstance(out, Compiled)
    (select,) = out.rules
    assert (select.id, select.scalar, select.op) == ("select", "top", "ge")
    assert out.metrics["tune"]["rules"]["select"]["rate"] <= 0.05
    with pytest.raises(ValueError, match="ACCEPT rule"):
        _compile(split, Target("risk", "false_pass", alpha=0.05), kind="choice")


def test_blocked_options_return_block_when_the_select_rule_fires() -> None:
    split = _multi(9, 3000, 3, sharpness=0.3)
    scored = compiler.score("choice", IDENTITY, split, Actions(block_options=(2,)))
    rules = [Rule("select", "SELECT", "top", "ge", 0.5)]
    action, _ = compiler.decide(rules, scored, Actions(block_options=(2,)))
    fired = scored.values["top"] >= 0.5
    assert set(action[fired & (scored.top == 2)]) == {2}  # BLOCK
    assert set(action[fired & (scored.top != 2)]) == {3}  # SELECT


@pytest.mark.req("FR-20")
def test_a_score_question_uses_tail_probabilities() -> None:
    split = _multi(10, 8000, 4, sharpness=0.3)
    actions = Actions(accept_below_level=2, block_from_level=3)
    out = _compile(split, Target("risk", "error_budget", alpha=0.05), actions, kind="score")
    assert isinstance(out, Compiled)
    scalars = {r.id: r.to_json()["scalar"] for r in out.rules}
    assert scalars["accept"] == {"tail_ge": 2}
    with pytest.raises(ValueError, match="accept_below_level"):
        compiler.templates("score", Actions())


@pytest.mark.req("FR-21")
@pytest.mark.req("FR-25")
def test_a_cost_target_minimises_cost_without_a_guarantee() -> None:
    tune = _noul(11, 5000, base=0.2)
    costs = Costs(escalation=1.0, wrong={"ACCEPT": 20.0, "BLOCK": 5.0})
    out = _compile(tune, Target("cost", costs=costs, max_escalation=0.3))
    assert isinstance(out, Compiled)
    assert out.guarantee == {
        "status": "none",
        "reason": "cost_target",
        "detail": "Expected-cost optimum on the tuning split.",
    }
    best = out.metrics["tune"]["expected_cost_per_1000"]
    assert out.metrics["tune"]["escalated"] <= 0.3
    scored = compiler.score("noul", IDENTITY, tune, BLOCK_HIGH)
    for lo, hi in [(0.05, 0.5), (0.1, 0.4), (0.02, 0.8)]:  # a few other choices cost more
        rules = [Rule("accept", "ACCEPT", "p", "lt", lo), Rule("block", "BLOCK", "p", "ge", hi)]
        other = compiler.simulate(rules, scored, BLOCK_HIGH, costs)
        if other["escalated"] <= 0.3:
            assert other["expected_cost_per_1000"] >= best - 1e-9


@pytest.mark.req("FR-14")
def test_unlabeled_fired_items_count_as_wrong() -> None:
    tune = _noul(12, 6000)
    labels = tune.labels.copy()
    low = tune.probs[:, 1] < np.quantile(tune.probs[:, 1], 0.3)
    labels[low & (np.arange(labels.size) % 10 == 0)] = NO_LABEL  # a tenth of the low scores
    holed = Split(tune.ids, tune.probs, labels, tune.audit)
    target = Target("risk", "false_pass", alpha=0.02)
    full, partial = _compile(tune, target), _compile(holed, target)
    assert isinstance(full, Compiled)
    assert isinstance(partial, Declined) or partial.rules[0].threshold < full.rules[0].threshold


@pytest.mark.req("FR-14")
def test_an_audited_only_path_counts_only_audited_items() -> None:
    tune = _noul(13, 6000)
    audited = np.zeros(tune.labels.size, bool)
    audited[::50] = True  # 2% audited: 120 items, under the 149 needed
    split = Split(tune.ids, tune.probs, tune.labels, audited)
    out = _compile(split, Target("risk", "false_pass", alpha=0.02), audited_only={"ACCEPT"})
    assert isinstance(out, Declined) and out.reason == "not_enough_labels"
    assert isinstance(_compile(split, Target("risk", "false_pass", alpha=0.02)), Compiled)


def test_targets_are_checked() -> None:
    for bad in (
        {"kind": "risk", "metric": "false_pass"},
        {"kind": "risk", "metric": "false_pass", "alpha": 1.5},
        {"kind": "risk", "metric": "false_pass", "alpha": 0.1, "delta": 0},
        {"kind": "cost"},
        {"kind": "cost", "costs": Costs(1, {}), "max_escalation": 2},
    ):
        with pytest.raises(ValueError):
            Target(**bad)  # type: ignore[arg-type]
    assert Target("risk", "false_pass", alpha=0.02, max_escalation=0.1).to_json() == {
        "kind": "risk", "metric": "false_pass", "alpha": 0.02, "delta": 0.05, "max_escalation": 0.1,
    }  # fmt: skip


@pytest.mark.req("NFR-09")
def test_the_certified_accept_rule_holds_on_fresh_traffic_most_of_the_time() -> None:
    """A small version of the coverage experiment (T2.5 runs 200 seeds nightly): the true
    false-pass rate exceeds alpha in at most about delta of trials."""
    alpha, violations, trials = 0.03, 0, 40
    for seed in range(trials):
        tune, fresh = _noul(100 + seed, 4000, base=0.1), _noul(10_000 + seed, 60_000, base=0.1)
        out = _compile(tune, Target("risk", "false_pass", alpha=alpha, delta=0.05))
        assert isinstance(out, Compiled)
        scored = compiler.score("noul", IDENTITY, fresh, BLOCK_HIGH)
        rate = compiler.simulate(out.rules, scored, BLOCK_HIGH)["rules"]["accept"]["rate"]
        violations += rate > alpha
    assert violations <= 6  # expected about 2 at delta = 0.05


@pytest.mark.req("FR-24")
def test_a_score_accept_rule_alone_names_the_cap_when_it_cant_meet_it() -> None:
    split = _multi(14, 6000, 4, sharpness=0.3)
    actions = Actions(accept_below_level=2)
    target = Target("risk", "false_pass", alpha=0.05, max_escalation=0.05)
    out = _compile(split, target, actions, kind="score")
    assert isinstance(out, Declined) and out.binding == "max_escalation"


@pytest.mark.req("FR-21")
def test_an_error_budget_leaves_out_a_rule_it_cant_certify() -> None:
    rng = np.random.default_rng(15)
    p = rng.beta(0.3, 3.0, size=8000)  # confident only about "no"
    y = (rng.random(p.size) < p).astype(np.intp)
    y[p > 0.5] = rng.random(int((p > 0.5).sum())) < 0.5  # the "yes" side is a coin flip
    split = Split(
        tuple(map(str, range(p.size))), np.column_stack([1 - p, p]), y, np.zeros(p.size, bool)
    )
    out = _compile(split, Target("risk", "error_budget", alpha=0.02))
    assert isinstance(out, Compiled)
    assert [r.id for r in out.rules] == ["accept"]
    assert any("block" in note for note in out.notes)


def test_a_target_with_costs_reports_them() -> None:
    costs = Costs(escalation=1.0, wrong={"ACCEPT": 5.0})
    target = Target("risk", "false_pass", alpha=0.02, costs=costs)
    assert target.to_json()["costs"] == {"escalation": 1.0, "wrong": {"ACCEPT": 5.0}}
    out = _compile(_noul(16, 6000), target)
    assert isinstance(out, Compiled) and out.metrics["eval"]["expected_cost_per_1000"] is not None

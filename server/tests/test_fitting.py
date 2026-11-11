"""Fits, compiles and the simulator without a database (T2.6)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from trimset_server import fitting
from trimset_server.records import QuestionIn
from trimset_stats.calibration import Calibrator
from trimset_stats.compiler import Actions
from trimset_stats.dataset import Item, build

NOW = datetime(2026, 11, 1, tzinfo=UTC)
NOUL = QuestionIn.model_validate(
    {
        "schema": "question/1",
        "type": "noul",
        "instructions": "Fraud?",
        "noul": {"true": None, "false": None},
    }
)
CHOICE = QuestionIn.model_validate(
    {"schema": "question/1", "type": "choice", "instructions": "Team?",
     "options": [{"value": v, "description": None} for v in ("billing", "shipping", "tech")]}
)  # fmt: skip


def _items(n: int, seed: int = 0, overconfident: float = 2.0) -> list[Item]:
    rng = np.random.default_rng(seed)
    q = rng.beta(0.5, 2.0, size=n)
    y = rng.random(n) < q
    p = 1 / (1 + np.exp(-overconfident * np.log(q / (1 - q))))
    return [
        Item(
            str(uuid.UUID(int=i)),
            (NOW - timedelta(days=1 + i % 20)).timestamp(),
            (1 - p[i], p[i]),
            int(y[i]),
        )
        for i in range(n)
    ]


def _fit(items: list[Item]) -> Any:
    return fitting.compute_fit(
        NOUL, "q_" + "a" * 32, "jev-1", items,
        fit_id=uuid.UUID(int=7), now=NOW, window_days=90, horizon_days=0,
    )  # fmt: skip


@pytest.mark.req("FR-16")
@pytest.mark.req("FR-17")
def test_a_fit_describes_its_map_its_data_and_its_effect(conforms: Any) -> None:
    out = _fit(_items(3000))
    assert isinstance(out, tuple)
    body, splits = out
    conforms(body, "fit")
    assert body["calibration"]["method"] != "identity"  # the scores were overconfident
    raw, calibrated = body["metrics"]["raw"], body["metrics"]["calibrated"]
    assert calibrated["ece_equal_width"] < raw["ece_equal_width"]
    assert body["provenance"]["seed"] == fitting.seed_of(uuid.UUID(int=7))
    assert sum(body["provenance"]["n"].values()) == 3000
    assert set(body["selection"]["cv_log_loss"]) == {"identity", "temperature", "platt", "isotonic"}
    tune, evaluation = fitting.unpack(splits)
    assert len(tune.labels) == body["provenance"]["n"]["tune"]
    assert len(evaluation.labels) == body["provenance"]["n"]["eval"]


@pytest.mark.req("FR-18")
def test_the_same_fit_id_gives_the_same_fit() -> None:
    first, again = _fit(_items(1500)), _fit(_items(1500))
    assert isinstance(first, tuple) and isinstance(again, tuple)
    assert first[0] == again[0]


@pytest.mark.req("FR-19")
def test_too_few_labels_say_how_many_more() -> None:
    out = _fit(_items(120))
    assert out == {"not_enough_labels": {"labeled": 120, "needed": 80}}


def test_choice_actions_name_options_by_value() -> None:
    actions = fitting.actions_of(CHOICE, {"actions": {"block_options": ["tech"]}})
    assert actions.block_options == (2,)
    with pytest.raises(fitting.JobError, match="isn't an option"):
        fitting.actions_of(CHOICE, {"actions": {"block_options": ["sales"]}})
    assert fitting.actions_of(NOUL, {}) == Actions()


def test_targets_come_from_the_policy_schema() -> None:
    target = fitting.target_of(
        {"kind": "risk", "metric": "false_pass", "alpha": 0.02, "max_escalation": 0.15}
    )
    assert (target.alpha, target.delta, target.max_escalation) == (0.02, 0.05, 0.15)
    cost = fitting.target_of({"kind": "cost", "costs": {"escalation": 1, "wrong": {"ACCEPT": 9}}})
    assert cost.costs is not None and cost.costs.wrong == {"ACCEPT": 9}
    for bad in (
        {"metric": "false_pass"},
        {"kind": "risk", "metric": "false_pass"},
        {"kind": "cost"},
    ):
        with pytest.raises(fitting.JobError):
            fitting.target_of(bad)


@pytest.mark.req("FR-26")
def test_the_simulator_grids_thresholds_or_takes_candidates() -> None:
    data = build(_items(2000), now=NOW.timestamp(), seed=1)
    cal = Calibrator("identity")
    two = fitting.simulate_rows(NOUL, cal, data.eval, Actions(), None, None)
    assert 1 < len(two) <= 22 * 22 and all(len(r["rules"]) == 2 for r in two)
    picked = [
        [
            fitting.rule_of(
                {"id": "accept", "action": "ACCEPT", "scalar": "p", "op": "lt", "threshold": 0.1}
            )
        ]
    ]
    (row,) = fitting.simulate_rows(NOUL, cal, data.eval, Actions(), picked, None)
    assert row["rules"][0]["threshold"] == 0.1 and row["metrics"]["n"] == len(data.eval.ids)
    with pytest.raises(fitting.JobError, match="scalar"):
        wrong = [
            [
                fitting.rule_of(
                    {
                        "id": "select",
                        "action": "SELECT",
                        "scalar": "top",
                        "op": "ge",
                        "threshold": 0.5,
                    }
                )
            ]
        ]
        fitting.simulate_rows(NOUL, cal, data.eval, Actions(), wrong, None)

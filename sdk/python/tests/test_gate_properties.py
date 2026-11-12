"""Property test for NFR-07: gate() never raises, whatever it is given, and always returns
one of the actions. Hypothesis is not installed in the Python 3.11 job, so it has a file of
its own; the rest of the chaos tests (test_chaos.py) run there."""

import json
import time
from pathlib import Path
from typing import Any

import pytest

hypothesis = pytest.importorskip("hypothesis")  # not installed in the Python 3.11 job
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from trimset import Client, GateResult  # noqa: E402
from trimset.client import ACTIONS, Snapshot, build_snapshot  # noqa: E402

GATES = json.loads(
    (Path(__file__).parents[3] / "docs" / "03-api" / "vectors" / "gates.json").read_text()
)
MODEL = "jev-1.14.0"
NAMES = ["refund_fraud", "team", "difficulty", "spam", "nope", ""]


def _offline() -> Client:
    client = Client("http://127.0.0.1:1", "tsi_test", poll_interval=3600, **GATES["client"])
    client.close(0)  # no threads, so no network at all
    client._snapshot = build_snapshot(GATES["bundle"], Snapshot(), time.time())
    return client


OFFLINE = _offline()

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=16),
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=6), inner, max_size=4)
    ),
    max_leaves=10,
)


@pytest.mark.req("NFR-07")
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    question=st.sampled_from(NAMES) | st.text(max_size=12) | json_values,
    p=st.none() | st.floats() | json_values,
    probabilities=st.none() | st.lists(st.floats(), max_size=6) | json_values,
    answer=st.none() | json_values | st.sampled_from([{"type": "noul", "noul": 0.5}]),
    model_served=st.none() | st.sampled_from([MODEL, "jev-1.15.0", ""]) | json_values,
    decision_id=st.none() | st.uuids().map(str) | st.text(max_size=40) | json_values,
    question_def=st.none() | json_values,
    default=st.none() | st.sampled_from([*ACTIONS, "accept", "SELECT:x", ""]) | json_values,
)
def test_gate_never_raises_and_always_returns_a_valid_action(**kwargs: Any) -> None:
    d = OFFLINE.gate(**kwargs)
    assert isinstance(d, GateResult)
    assert d.action in ACTIONS or d.action.startswith("SELECT:")
    assert d.source in ("policy", "audit", "default")
    assert isinstance(d.decision_id, str) and d.decision_id
    if d.source == "default":
        assert d.reason and d.action in ACTIONS

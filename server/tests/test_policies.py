"""The lifecycle's transition table (FR-27), and the bundle's If-None-Match (FR-32)."""

import pytest

from trimset_server.bundle import matches
from trimset_server.policies import STATES, TRANSITIONS, next_state

ALLOWED = {
    ("submit", "draft"): "proposed",
    ("approve", "proposed"): "approved",
    ("reject", "proposed"): "rejected",
    ("activate", "approved"): "active",
    ("retire", "active"): "retired",
}


@pytest.mark.req("FR-27")
def test_only_the_lifecycle_transitions_are_allowed() -> None:
    for action in TRANSITIONS:
        for state in STATES:
            assert next_state(action, state) == ALLOWED.get((action, state)), (action, state)


def test_if_none_match_takes_strong_weak_lists_and_star() -> None:
    assert not matches(None, "ab") and not matches("", "ab")
    assert matches('"ab"', "ab") and matches('W/"ab"', "ab")
    assert matches('"zz", W/"ab"', "ab") and matches("*", "ab")
    assert not matches('"abc"', "ab")

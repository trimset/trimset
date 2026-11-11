"""The lifecycle's transition table (FR-27)."""

import pytest

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

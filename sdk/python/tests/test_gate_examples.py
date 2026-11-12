"""The reference gate() examples (docs/03-api/vectors/gates.json), which every SDK must
reproduce. Their expected results come from trimset-stats and sdk.md §3, not from an SDK."""

import json
import time
from pathlib import Path
from typing import Any

import pytest

from trimset import Client
from trimset.client import Snapshot, build_snapshot

GATES = json.loads(
    (Path(__file__).parents[3] / "docs" / "03-api" / "vectors" / "gates.json").read_text()
)


@pytest.fixture(scope="module")
def client() -> Client:
    c = Client("http://127.0.0.1:1", "tsi_test", poll_interval=3600, **GATES["client"])
    c.close(0)
    c._snapshot = build_snapshot(GATES["bundle"], Snapshot(), time.time())
    return c


@pytest.mark.req("FR-33")
@pytest.mark.req("FR-35")
@pytest.mark.parametrize("example", GATES["examples"], ids=lambda e: e["name"])
def test_the_reference_examples(client: Client, example: dict[str, Any]) -> None:
    d = client.gate(**example["gate"])
    want = example["expected"]
    got = {
        k: getattr(d, k) for k in ("action", "label", "source", "reason", "policy_version", "audit")
    }
    assert got == {k: v for k, v in want.items() if k != "scalar"}
    if want["scalar"] is None:
        assert d.scalar is None
    else:
        assert d.scalar == pytest.approx(want["scalar"], abs=1e-12)
    if "decision_id" in example["gate"]:
        assert d.decision_id == example["gate"]["decision_id"]

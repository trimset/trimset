"""Every value in docs/03-api/vectors/vectors.json, reproduced by the SDK (hashing.md)."""

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from trimset.hashing import (
    bundle_etag,
    canonical,
    fingerprint,
    project_state_key,
    question_id,
    state_hash,
)
from trimset.wire import question_from_openai, question_from_systemone

API = Path(__file__).resolve().parents[3] / "docs" / "03-api"
VECTORS = json.loads((API / "vectors" / "vectors.json").read_text(encoding="utf-8"))


def _example(name: str) -> Any:
    return json.loads((API / "examples" / name).read_text(encoding="utf-8"))


@pytest.mark.req("FR-03")
@pytest.mark.parametrize("case", VECTORS["question_ids"], ids=lambda c: c["case"])
def test_question_vectors(case: dict[str, Any]) -> None:
    if case["format"] == "systemone":
        definition = question_from_systemone(case["name"], case["wire"])
    else:
        definition = question_from_openai(case["wire"])
    assert definition == case["definition"]
    assert canonical(definition).decode("utf-8") == case["canonical"]
    assert question_id(definition) == case["question_id"]


@pytest.mark.req("FR-09")
def test_state_hash_vectors() -> None:
    s = VECTORS["state_hashes"]
    key = project_state_key(s["server_secret_utf8"].encode("utf-8"), s["project_id"])
    assert base64.b64encode(key).decode("ascii") == s["project_key_b64"]
    for case in s["cases"]:
        assert canonical(case["state"]).decode("utf-8") == case["canonical"]
        assert state_hash(key, case["state"]) == case["state_hash"], case["case"]


def test_project_key_ignores_uuid_case() -> None:
    s = VECTORS["state_hashes"]
    secret = s["server_secret_utf8"].encode("utf-8")
    assert project_state_key(secret, s["project_id"].upper()) == project_state_key(
        secret, s["project_id"]
    )


def test_policy_fingerprint_and_bundle_etag_match_the_examples() -> None:
    policy, bundle = _example("policy.json"), _example("bundle.json")
    assert fingerprint(policy) == policy["fingerprint"] == VECTORS["fingerprints"][0]["fingerprint"]
    assert bundle_etag(bundle) == bundle["etag"]


def test_a_changed_policy_changes_its_fingerprint() -> None:
    policy = _example("policy.json")
    policy["rules"][0]["threshold"] = 0.31
    assert fingerprint(policy) != policy["fingerprint"]


def test_values_json_cannot_carry_exactly_are_refused() -> None:
    with pytest.raises(ValueError):
        canonical({"x": float("nan")})

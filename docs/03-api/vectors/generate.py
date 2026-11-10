"""Reference computation of trimset ids and hashes, and the test vectors in vectors.json.

This is spec tooling: it defines the expected values the server and every SDK must
reproduce (docs/03-api/hashing.md). It is not the product implementation.

    uv run --with rfc8785==0.1.4 python docs/03-api/vectors/generate.py          # rewrite vectors.json
    uv run --with rfc8785==0.1.4 python docs/03-api/vectors/generate.py --check  # verify, change nothing
"""

import base64
import hashlib
import hmac
import json
import pathlib
import sys
import unicodedata

import rfc8785

HERE = pathlib.Path(__file__).parent
API = HERE.parent


def nfc(value):
    """Unicode NFC on every string and object key, recursively."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [nfc(v) for v in value]
    if isinstance(value, dict):
        return {nfc(k): nfc(v) for k, v in value.items()}
    return value


def canonical(value) -> bytes:
    return rfc8785.dumps(nfc(value))


def _instructions(q: dict, name):
    instructions = q.get("instructions")
    return instructions if instructions is not None else {"$name": name}


def question_from_systemone(name: str, q: dict) -> dict:
    d = {"schema": "question/1", "type": q["type"], "instructions": _instructions(q, name)}
    criteria = q.get("criteria")
    if q["type"] == "noul":
        criteria = criteria or {}
        d["noul"] = {"true": criteria.get("true"), "false": criteria.get("false")}
    elif q["type"] == "choice":
        d["options"] = [{"value": k, "description": v} for k, v in criteria.items()]
    elif q["type"] == "score":
        d["levels"] = [{"label": None, "description": v} for v in criteria]
    else:
        raise ValueError(f"unsupported question type {q['type']!r}")
    return d


OPENAI_TYPES = {"predicate": "noul", "choice": "choice", "score": "score"}


def question_from_openai(q: dict) -> dict:
    kind = OPENAI_TYPES[q["type"]]
    d = {"schema": "question/1", "type": kind, "instructions": _instructions(q, q.get("name"))}
    if kind == "noul":
        d["noul"] = {"true": None, "false": None}
    elif kind == "choice":
        d["options"] = [{"value": c["value"], "description": c.get("description")} for c in q["choices"]]
    else:
        d["levels"] = [{"label": lv["label"], "description": lv.get("description")} for lv in q["levels"]]
    return d


def question_id(definition: dict) -> str:
    return "q_" + hashlib.sha256(canonical(definition)).hexdigest()[:32]


def project_state_key(server_secret: bytes, project_id: str) -> bytes:
    return hmac.new(server_secret, project_id.encode("utf-8"), hashlib.sha256).digest()


def state_hash(key: bytes, state) -> str:
    return hmac.new(key, canonical(state), hashlib.sha256).hexdigest()


def fingerprint(policy: dict) -> str:
    body = {k: v for k, v in policy.items() if k != "fingerprint"}
    return "sha256:" + hashlib.sha256(canonical(body)).hexdigest()


def bundle_etag(bundle: dict) -> str:
    body = {k: v for k, v in bundle.items() if k not in ("generated_at", "etag")}
    return hashlib.sha256(canonical(body)).hexdigest()


FRAUD = "Is this refund request fraudulent?"
TRIAGE = {"billing": "Payments and refunds", "shipping": None, "returns": None, "tech": "Software errors"}
SEVERITY = ["No problem", "Mild problem", "Serious problem", "Must be removed"]

QUESTION_CASES = [
    ("noul_systemone", "systemone", "fraud", {"type": "noul", "instructions": FRAUD}),
    ("noul_openai_same_question", "openai", None, {"type": "predicate", "name": "fraud", "instructions": FRAUD}),
    ("noul_with_criteria", "systemone", "fraud",
     {"type": "noul", "instructions": FRAUD, "criteria": {"true": "Clear signs of fraud", "false": "Looks legitimate"}}),
    ("noul_trailing_space_is_different", "systemone", "fraud", {"type": "noul", "instructions": FRAUD + " "}),
    ("noul_nfc_composed", "systemone", "cafe", {"type": "noul", "instructions": "Is this café order fraudulent?"}),
    ("noul_nfc_decomposed_same_id", "systemone", "cafe", {"type": "noul", "instructions": "Is this café order fraudulent?"}),
    ("noul_no_instructions_uses_name", "systemone", "billing", {"type": "noul"}),
    ("choice_systemone", "systemone", "team",
     {"type": "choice", "instructions": "Which team should handle this ticket?", "criteria": TRIAGE}),
    ("choice_reordered_is_different", "systemone", "team",
     {"type": "choice", "instructions": "Which team should handle this ticket?",
      "criteria": {k: TRIAGE[k] for k in ["shipping", "billing", "returns", "tech"]}}),
    ("choice_openai_same_question", "openai", None,
     {"type": "choice", "name": "team", "instructions": "Which team should handle this ticket?",
      "choices": [{"value": "billing", "description": "Payments and refunds"}, {"value": "shipping"},
                  {"value": "returns"}, {"value": "tech", "description": "Software errors"}]}),
    ("choice_openai_boolean_value", "openai", None,
     {"type": "choice", "name": "ok", "instructions": "Approve?", "choices": [{"value": True}, {"value": False}]}),
    ("choice_openai_string_value_is_different", "openai", None,
     {"type": "choice", "name": "ok", "instructions": "Approve?", "choices": [{"value": "true"}, {"value": "false"}]}),
    ("score_systemone", "systemone", "severity",
     {"type": "score", "instructions": "How serious is the problem with this listing?", "criteria": SEVERITY}),
    ("score_openai_with_labels_is_different", "openai", None,
     {"type": "score", "name": "severity", "instructions": "How serious is the problem with this listing?",
      "levels": [{"label": "none", "description": SEVERITY[0]}, {"label": "mild", "description": SEVERITY[1]},
                 {"label": "serious", "description": SEVERITY[2]}, {"label": "remove", "description": SEVERITY[3]}]}),
]

TEST_SECRET = b"trimset-test-secret-do-not-use"
TEST_PROJECT = "0193f0a2-7c4e-7b1a-9d2e-5f6a7b8c9d0e"
STATE_CASES = [
    ("object_state", {"refund_id": "rf-1001", "amount": 129.5, "reason": "Item never arrived"}),
    ("same_object_other_key_order", {"reason": "Item never arrived", "amount": 129.5, "refund_id": "rf-1001"}),
    ("string_state", "Our checkout has returned 500 errors since 9am."),
    ("array_state", [{"role": "user", "content": "Where is my order?"}]),
]


def build_vectors() -> dict:
    questions = []
    for case, fmt, name, wire in QUESTION_CASES:
        definition = question_from_systemone(name, wire) if fmt == "systemone" else question_from_openai(wire)
        questions.append({
            "case": case, "format": fmt, "name": name, "wire": wire, "definition": definition,
            "canonical": canonical(definition).decode("utf-8"), "question_id": question_id(definition),
        })
    key = project_state_key(TEST_SECRET, TEST_PROJECT)
    states = [{
        "case": case, "state": state, "canonical": canonical(state).decode("utf-8"),
        "state_hash": state_hash(key, state),
    } for case, state in STATE_CASES]
    policy = json.loads((API / "examples" / "policy.json").read_text(encoding="utf-8"))
    return {
        "schema": "vectors/1",
        "question_ids": questions,
        "state_hashes": {
            "server_secret_utf8": TEST_SECRET.decode("utf-8"), "project_id": TEST_PROJECT,
            "project_key_b64": base64.b64encode(key).decode("ascii"), "cases": states,
        },
        "fingerprints": [{"file": "examples/policy.json", "fingerprint": fingerprint(policy)}],
    }


def check(vectors: dict) -> list[str]:
    problems = []
    stored = json.loads((HERE / "vectors.json").read_text(encoding="utf-8"))
    if stored != vectors:
        problems.append("vectors.json is out of date: run generate.py")
    ids = {v["case"]: v["question_id"] for v in vectors["question_ids"]}
    same = [("noul_systemone", "noul_openai_same_question"), ("noul_nfc_composed", "noul_nfc_decomposed_same_id"),
            ("choice_systemone", "choice_openai_same_question")]
    differ = [("noul_systemone", "noul_with_criteria"), ("noul_systemone", "noul_trailing_space_is_different"),
              ("choice_systemone", "choice_reordered_is_different"),
              ("choice_openai_boolean_value", "choice_openai_string_value_is_different"),
              ("score_systemone", "score_openai_with_labels_is_different")]
    problems += [f"{a} and {b} should share an id" for a, b in same if ids[a] != ids[b]]
    problems += [f"{a} and {b} should differ" for a, b in differ if ids[a] == ids[b]]
    hashes = {c["case"]: c["state_hash"] for c in vectors["state_hashes"]["cases"]}
    if hashes["object_state"] != hashes["same_object_other_key_order"]:
        problems.append("key order changed the state hash")
    policy = json.loads((API / "examples" / "policy.json").read_text(encoding="utf-8"))
    if policy["fingerprint"] != fingerprint(policy):
        problems.append(f"examples/policy.json fingerprint should be {fingerprint(policy)}")
    bundle = json.loads((API / "examples" / "bundle.json").read_text(encoding="utf-8"))
    if bundle["etag"] != bundle_etag(bundle):
        problems.append(f"examples/bundle.json etag should be {bundle_etag(bundle)}")
    for f in ("record.json", "policy.json", "fit.json", "drift_report.json"):
        doc = json.loads((API / "examples" / f).read_text(encoding="utf-8"))
        if doc["question_id"] not in ids.values():
            problems.append(f"examples/{f} uses a question_id that is not a test vector")
    return problems


if __name__ == "__main__":
    vectors = build_vectors()
    if "--check" in sys.argv:
        problems = check(vectors)
        print("\n".join(problems) or "all vectors and examples check out")
        sys.exit(1 if problems else 0)
    (HERE / "vectors.json").write_text(json.dumps(vectors, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for v in vectors["question_ids"]:
        print(f"{v['question_id']}  {v['case']}")
    print("fingerprint", vectors["fingerprints"][0]["fingerprint"])

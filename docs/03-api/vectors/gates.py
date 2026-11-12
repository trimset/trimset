"""Reference gate() examples (sdk.md §3): fixed inputs, and the result every SDK returns.

Spec tooling, like generate.py. A policy's decision is computed with trimset-stats, the
numpy implementation the compiler certifies with, never with an SDK; the defaults follow
sdk.md §3's rules, written out by hand. The fingerprints come from generate.py. The Python
SDK's tests read gates.json, and the TypeScript SDK's will.

    uv run python docs/03-api/vectors/gates.py          # rewrite gates.json
    uv run python docs/03-api/vectors/gates.py --check  # verify (a server test runs it)
"""

import copy
import difflib
import hashlib
import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from generate import bundle_etag, fingerprint, question_id

from trimset_stats import compiler
from trimset_stats.calibration import Calibrator
from trimset_stats.compiler import Actions, Rule
from trimset_stats.dataset import Split

HERE = pathlib.Path(__file__).parent
OUT = HERE / "gates.json"
MODEL = "jev-1.14.0"
EXAMPLE = json.loads((HERE.parent / "examples" / "bundle.json").read_text())


def _question(definition: dict) -> tuple[str, dict]:
    return question_id(definition), definition


def _policy(qid: str, name: str, kind: str, version: int, **fields: object) -> dict:
    body = copy.deepcopy(EXAMPLE["policies"][0]["policy"])
    body |= {"question_id": qid, "question_name": name, "type": kind, "version": version}
    body |= {"policy_id": f"019a5c00-2222-7aaa-8bbb-{version:012d}", "audit_fraction": 0}
    body |= fields
    body["fingerprint"] = fingerprint(body)
    return body


def _bundle() -> dict:
    refund = EXAMPLE["questions"]["refund_fraud"]
    team = _question({
        "schema": "question/1", "type": "choice", "instructions": "Which team should handle this?",
        "options": [{"value": v, "description": None} for v in ("billing", "tech", True)],
    })  # fmt: skip
    difficulty = _question({
        "schema": "question/1", "type": "score", "instructions": "How hard is this request?",
        "levels": [{"label": str(i), "description": None} for i in range(4)],
    })  # fmt: skip
    spam = _question({
        "schema": "question/1", "type": "noul", "instructions": "Is this message spam?",
        "noul": {"true": None, "false": None},
    })  # fmt: skip
    select = {"id": "select", "action": "SELECT", "scalar": "top", "op": "ge", "threshold": 0.6}
    policies = [
        EXAMPLE["policies"][0]["policy"],  # refund_fraud: Platt, 1% audited (sdk.md §3)
        _policy(team[0], "team", "choice", 21,
                calibration={"method": "temperature", "temperature": 0.8},
                rules=[select | {"per_option": [{"value": "tech", "threshold": 0.9}]}],
                option_actions=["billing"]),
        _policy(difficulty[0], "difficulty", "score", 31, calibration={"method": "identity"},
                rules=[
                    {"id": "accept", "action": "ACCEPT", "scalar": {"tail_ge": 2}, "op": "lt",
                     "threshold": 0.1},
                    {"id": "block", "action": "BLOCK", "scalar": {"tail_ge": 3}, "op": "ge",
                     "threshold": 0.5},
                ]),
        _policy(spam[0], "spam", "noul", 41,
                calibration={"method": "isotonic", "x": [0.0, 0.2, 0.5, 0.9, 1.0],
                             "y": [0.0, 0.05, 0.3, 0.9, 1.0]}),
    ]  # fmt: skip
    tampered = _policy(spam[0], "spam", "noul", 42, model_served="jev-1.15.0")
    tampered["rules"][0]["threshold"] = 0.99  # changed after fingerprinting
    entries = [{"policy": p, "guarantee_status": "certified"} for p in [*policies, tampered]]
    questions = {"refund_fraud": refund, "team": _entry(team), "difficulty": _entry(difficulty),
                 "spam": _entry(spam)}  # fmt: skip
    bundle = EXAMPLE | {"questions": questions, "policies": entries}
    return bundle | {"etag": bundle_etag(bundle)}


def _entry(q: tuple[str, dict]) -> dict:
    return {"question_id": q[0], "definition": q[1]}


def _u(decision_id: str) -> float:
    return int(hashlib.sha256(decision_id.lower().encode()).hexdigest()[:16], 16) / 2**64


def _ids() -> tuple[str, str]:
    """A decision id the 1% audit leaves alone, and one it samples (sdk.md §3 step 4)."""
    ids = [f"019a6f4e-0000-7000-8000-{i:012d}" for i in range(1000)]
    return next(i for i in ids if _u(i) >= 0.01), next(i for i in ids if _u(i) < 0.01)


def _reference(policy: dict, definition: dict, probs: list[float]) -> dict:
    """The policy's decision, with numpy: trimset-stats' score and decide."""
    kind = policy["type"]
    cal = Calibrator.from_json(policy["calibration"])
    rules = [_rule(r) for r in policy["rules"]]
    values = [o["value"] for o in definition.get("options", [])]
    blocked = tuple(i for i, v in enumerate(values) if v in policy.get("option_actions", []))
    tails = [r.scalar for r in rules if isinstance(r.scalar, int)]
    actions = Actions(
        block_options=blocked,
        accept_below_level=tails[0] if tails else None,
        block_from_level=tails[-1] if len(tails) > 1 else None,
    )
    row = np.array([[1 - probs[0], probs[0]]] if kind == "noul" else [probs], dtype=np.float64)
    split = Split(("x",), row, np.zeros(1, np.intp), np.zeros(1, bool))
    scored = compiler.score(kind, cal, split, actions)
    if kind == "choice" and policy["rules"][0].get("per_option"):
        top = values[int(scored.top[0])]
        for option in policy["rules"][0]["per_option"]:
            if option["value"] == top and type(option["value"]) is type(top):
                rules = [Rule(r.id, r.action, r.scalar, r.op, option["threshold"]) for r in rules]
    codes, by = compiler.decide(rules, scored, actions)
    code, rule = int(codes[0]), rules[by[0]] if by[0] >= 0 else rules[0]
    label = values[int(scored.top[0])] if kind == "choice" and code in (2, 3) else None
    action = {0: "ESCALATE", 1: "ACCEPT", 2: "BLOCK"}.get(code)
    if code == 3:
        action = "SELECT:" + (label if isinstance(label, str) else json.dumps(label))
    if code == 2 and label is not None and by[0] >= 0 and rules[by[0]].action != "SELECT":
        label = None
    return {"action": action, "label": label, "source": "policy", "reason": None,
            "policy_version": policy["version"], "audit": False,
            "scalar": _scalar(scored.values[rule.scalar][0])}  # fmt: skip


def _scalar(value: float) -> float:
    """A scalar to 10 decimals: numpy's exp and log differ in the last bit between
    platforms, so the file must not depend on it. Decisions never sit that close to a
    threshold (thresholds move off the data, ADR 0006), and SDKs compare with 1e-9."""
    return round(float(value), 10)


def _rule(raw: dict) -> Rule:
    scalar = raw["scalar"]
    return Rule(raw["id"], raw["action"], scalar["tail_ge"] if isinstance(scalar, dict) else
                scalar, raw["op"], raw["threshold"])  # fmt: skip


def _default(action: str, reason: str) -> dict:
    return {"action": action, "label": None, "source": "default", "reason": reason,
            "policy_version": None, "audit": False, "scalar": None}  # fmt: skip


def build() -> dict:
    bundle = _bundle()
    by_name = {e["policy"]["question_name"]: e["policy"] for e in bundle["policies"][:4]}
    defs = {n: q["definition"] for n, q in bundle["questions"].items()}
    plain, audited = _ids()
    examples = []

    def policy_case(name: str, question: str, probs: list[float], **gate: object) -> None:
        total = sum(probs)
        normal = probs if len(probs) == 1 else [p / total for p in probs]
        expected = _reference(by_name[question], defs[question], normal)
        examples.append({"name": name, "gate": {"question": question, "model_served": MODEL,
                         "decision_id": plain} | gate, "expected": expected})  # fmt: skip

    policy_case("sdk.md §3: Platt maps 0.83 to 0.6695, between the rules", "refund_fraud",
                [0.83], p=0.83)  # fmt: skip
    policy_case("below the accept threshold", "refund_fraud", [0.1], p=0.1)
    policy_case("above the block threshold", "refund_fraud", [0.99], p=0.99)
    policy_case("a systemone answer", "refund_fraud", [0.83],
                answer={"type": "noul", "noul": 0.83})  # fmt: skip
    policy_case("an OpenAI decisions answer", "refund_fraud", [0.83],
                answer={"type": "predicate", "probability": 0.83})  # fmt: skip
    policy_case("the top option is a boolean", "team", [0.1, 0.2, 0.7],
                probabilities=[0.1, 0.2, 0.7])  # fmt: skip
    policy_case("an option in option_actions blocks", "team", [0.7, 0.2, 0.1],
                probabilities=[0.7, 0.2, 0.1])  # fmt: skip
    policy_case("per_option raises tech's threshold", "team", [0.1, 0.8, 0.1],
                probabilities=[0.1, 0.8, 0.1])  # fmt: skip
    policy_case("tech clears its own threshold", "team", [0.05, 0.95, 0.0],
                probabilities=[0.05, 0.95, 0.0])  # fmt: skip
    policy_case("a tie goes to the first option", "team", [0.4, 0.4, 0.2],
                probabilities=[0.4, 0.4, 0.2])  # fmt: skip
    policy_case("probabilities are divided by their sum", "team", [0.1004, 0.2, 0.7],
                probabilities=[0.1004, 0.2, 0.7])  # fmt: skip
    policy_case("a low tail accepts", "difficulty", [0.6, 0.35, 0.04, 0.01],
                probabilities=[0.6, 0.35, 0.04, 0.01])  # fmt: skip
    policy_case("a high tail blocks", "difficulty", [0.1, 0.1, 0.2, 0.6],
                probabilities=[0.1, 0.1, 0.2, 0.6])  # fmt: skip
    policy_case("in between escalates", "difficulty", [0.3, 0.3, 0.3, 0.1],
                probabilities=[0.3, 0.3, 0.3, 0.1])  # fmt: skip
    policy_case("isotonic interpolates between breakpoints", "spam", [0.42], p=0.42)

    refund = by_name["refund_fraud"]
    examples.append({
        "name": "1% of decision ids are audited, by their hash",
        "gate": {"question": "refund_fraud", "p": 0.1, "model_served": MODEL,
                 "decision_id": audited},
        "expected": {"action": "ESCALATE", "label": None, "source": "audit", "reason": None,
                     "policy_version": refund["version"], "audit": True, "scalar": None},
    })  # fmt: skip
    changed = defs["refund_fraud"] | {"instructions": "Is this refund fraud?"}
    for name, gate, expected in [
        ("an unknown question", {"question": "nope", "p": 0.5, "model_served": MODEL},
         _default("ESCALATE", "no_policy")),
        ("no model, and no default_model", {"question": "refund_fraud", "p": 0.5},
         _default("ACCEPT", "model_unknown")),
        ("a policy, but for another model", {"question": "refund_fraud", "p": 0.5,
                                             "model_served": "jev-1.15.0"},
         _default("ACCEPT", "model_mismatch")),
        ("a probability outside [0, 1]", {"question": "refund_fraud", "p": 1.5,
                                          "model_served": MODEL},
         _default("ACCEPT", "invalid_probabilities")),
        ("probabilities that don't sum to 1", {"question": "team", "probabilities": [0.5, 0.6, 0.1],
                                               "model_served": MODEL},
         _default("ESCALATE", "invalid_probabilities")),
        ("a refusal", {"question": "refund_fraud", "answer": {"type": "refusal"},
                       "model_served": MODEL},
         _default("ACCEPT", "refusal")),
        ("the question was reworded", {"question": "refund_fraud", "p": 0.5, "model_served": MODEL,
                                       "question_def": changed},
         _default("ACCEPT", "question_changed")),
        ("the only policy failed its fingerprint", {"question": "spam", "p": 0.5,
                                                    "model_served": "jev-1.15.0"},
         _default("ESCALATE", "fingerprint_mismatch")),
    ]:  # fmt: skip
        examples.append({"name": name, "gate": gate, "expected": expected})
    return {
        "schema": "gate-examples/1",
        "client": {"defaults": {"refund_fraud": "ACCEPT"}, "default_model": None},
        "bundle": bundle,
        "examples": examples,
    }


if __name__ == "__main__":
    text = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
    if "--check" in sys.argv:
        have = OUT.read_text()
        if have != text:
            diff = difflib.unified_diff(have.splitlines(), text.splitlines(), "gates.json",
                                        "gates.py now", lineterm="", n=1)  # fmt: skip
            sys.exit("gates.json is stale: run gates.py\n" + "\n".join(list(diff)[:60]))
        print("gates.json is current")
    else:
        OUT.write_text(text)
        print(f"wrote {OUT}")

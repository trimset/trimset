"""Decisions for agents (TV.1, ADR 0014): ask a decision model whether a tool call is safe
to run without a human, and let the project's approved policy turn its answer into
run, ask a person, or block.

    asked = ask_model(url, key, "jev-latest", {"tool": "Bash", "input": {"command": "ls"}})
    verdict = decide(client, state, model="jev-latest")      # asks, then gates

The question is "Is this tool call safe to run without a human?" and its probability is
that of *safe*, so the question's policy is compiled with `high_means: accept` (UC-5).

Fail safe, never fail open. Anything that goes wrong (the model, the network, no policy,
a changed question) is a verdict of ESCALATE: a person decides. A default of ACCEPT is
never honoured, whatever the client was configured with. Used by the Claude Code hook
(hook.py) and the MCP server (mcp.py); stdlib only, like the rest of the SDK.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trimset.client import Client, GateResult, _open
from trimset.policy import apply, describe_rule
from trimset.wire import question_from_systemone

QUESTION = "safe_to_run"
INSTRUCTIONS = "Is this tool call safe to run without a human?"
CLAUDE_CODE = {"ACCEPT": "allow", "ESCALATE": "ask", "BLOCK": "deny"}  # PreToolUse decisions
STATE_LIMIT = 20_000  # the longest string in a tool call that trimset will vouch for
WHY_DEFAULT = {
    "no_policy": "there is no approved policy for this question yet",
    "model_unknown": "the model's version is unknown",
    "model_mismatch": "the model answered with a version the policy was not compiled for",
    "question_changed": "the question's wording changed since the policy was compiled",
    "invalid_probabilities": "the model's answer was not a valid probability",
    "fingerprint_mismatch": "the policy failed its integrity check",
    "stale_policy": "the policy could not be refreshed recently enough",
    "refusal": "the model declined to answer",
    "model_unreachable": "the decision model could not be reached",
    "too_long": "the call is too long to check in full",
}


class AgentError(Exception):
    """The decision model could not be asked, or its answer was unusable."""


@dataclass(frozen=True)
class Asked:
    probability: float  # that the tool call is safe
    model_served: str
    decision_id: str | None  # from the proxy, so the decision is logged exactly once


@dataclass(frozen=True)
class Verdict:
    action: str  # ACCEPT, ESCALATE or BLOCK
    reason: str  # a sentence a person can read
    source: str  # policy, audit or default
    policy_version: int | None = None
    probability: float | None = None
    calibrated: float | None = None
    decision_id: str | None = None
    why_default: str | None = None

    @property
    def claude_code(self) -> str:
        """allow, ask or deny: a PreToolUse permission decision."""
        return CLAUDE_CODE[self.action]


def question_definition(name: str = QUESTION, instructions: str = INSTRUCTIONS) -> dict[str, Any]:
    """The question/1 the policy is bound to: an edit to its wording makes a new question."""
    return question_from_systemone(name, {"type": "noul", "instructions": instructions})


def longest_string(value: Any) -> int:
    """The length of the longest string anywhere in `value`."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, Mapping):
        return max((longest_string(v) for v in value.values()), default=0)
    if isinstance(value, list | tuple):
        return max((longest_string(v) for v in value), default=0)
    return 0


def shorten(value: Any, limit: int = STATE_LIMIT) -> Any:
    """`value` with every string over `limit` cut: a last guard on what ask_model sends.
    `decide` never reaches it with a long string, because a call it can't see in full is not
    one it may vouch for: a decision about the first part says nothing about the rest."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"...[{len(value) - limit} more]"
    if isinstance(value, Mapping):
        return {str(k): shorten(v, limit) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [shorten(v, limit) for v in value]
    return value


def ask_model(
    base_url: str,
    key: str,
    model: str,
    state: Any,
    *,
    name: str = QUESTION,
    instructions: str = INSTRUCTIONS,
    timeout: float = 10.0,
) -> Asked:
    """Asks the decision model through trimset's proxy, which forwards the call unchanged
    and logs the decision. Raises AgentError."""
    if urllib.parse.urlsplit(base_url).scheme not in ("http", "https"):
        raise AgentError("the trimset URL must start with http:// or https://")
    body = {
        "model": model,
        "state": shorten(state),
        "questions": {name: {"type": "noul", "instructions": instructions}},
    }
    request = urllib.request.Request(  # noqa: S310 - http(s) only, checked above
        base_url.rstrip("/") + "/v1/systemone",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "X-Trimset-Key": key,
            "Content-Type": "application/json",
            "User-Agent": "trimset-agent",
        },
    )
    try:
        with _open(request, timeout=timeout) as response:
            raw = response.read()
            ids = response.headers.get("X-Trimset-Decision-Id") or ""
    except urllib.error.HTTPError as e:
        raise AgentError(f"trimset answered {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AgentError(f"can't reach {base_url}: {getattr(e, 'reason', e)}") from None
    try:
        answer = json.loads(raw)["answers"][name]
        p, served = answer["noul"], json.loads(raw)["model"]
    except (ValueError, KeyError, TypeError):
        raise AgentError("the model's answer has no noul probability for the question") from None
    if isinstance(p, bool) or not isinstance(p, int | float) or not 0 <= p <= 1:
        raise AgentError("the model's probability is not a number between 0 and 1")
    if not isinstance(served, str) or not served:
        raise AgentError("the model's answer names no model")
    return Asked(float(p), served, ids.split(",")[0].strip() or None)


def decide(
    client: Client,
    state: Any,
    *,
    model: str,
    name: str = QUESTION,
    instructions: str = INSTRUCTIONS,
    timeout: float = 10.0,
) -> Verdict:
    """Asks the model about `state`, then gates its answer. Never raises.

    A call with a string over STATE_LIMIT is not asked about at all: the model would see only
    the start, and "safe" for the start is not "safe" for the call (a harmless command padded
    until the dangerous part falls off the end). It goes to a person."""
    if (n := longest_string(state)) > STATE_LIMIT:
        return _ask_a_person(
            "too_long", f"{n:,} characters in one field, the limit is {STATE_LIMIT:,}"
        )
    try:
        asked = ask_model(
            client.base_url, client._key, model, state,
            name=name, instructions=instructions, timeout=timeout,
        )  # fmt: skip
    except AgentError as e:
        return _ask_a_person("model_unreachable", str(e))
    except Exception as e:  # nothing here may stop the agent, or let a call through
        return _ask_a_person("model_unreachable", type(e).__name__)
    try:
        gated = client.gate(
            name,
            p=asked.probability,
            model_served=asked.model_served,
            decision_id=asked.decision_id,
            question_def=question_definition(name, instructions),
            default="ESCALATE",
        )
    except Exception as e:
        return _ask_a_person("model_unreachable", type(e).__name__)
    return verdict_of(gated, asked)


def _ask_a_person(why: str, detail: str = "") -> Verdict:
    reason = f"Needs a person: {WHY_DEFAULT[why]}" + (f" ({detail})." if detail else ".")
    return Verdict("ESCALATE", reason, "default", why_default=why)


def verdict_of(gated: GateResult, asked: Asked) -> Verdict:
    """A gate result as a verdict. A default is always ESCALATE: asking is the safe
    answer when trimset has nothing approved to go on."""
    p, s, v = asked.probability, gated.scalar, gated.policy_version
    common: dict[str, Any] = {
        "policy_version": v,
        "probability": p,
        "calibrated": s,
        "decision_id": asked.decision_id,  # the proxy's: the id of a logged decision, or None
    }
    if gated.source == "default":
        why = gated.reason or "no_policy"
        reason = (
            f"Needs a person: {WHY_DEFAULT.get(why, why)}, so trimset asks instead of guessing."
        )
        return Verdict("ESCALATE", reason, "default", why_default=why, **common)
    if gated.source == "audit":
        reason = f"Sampled for a person to check (audit, policy v{v}), whatever the model said."
        return Verdict("ESCALATE", reason, "audit", **common)
    said = f"the model says {p:.0%} safe" + (f" (calibrated {s:.0%})" if s is not None else "")
    if gated.action == "ACCEPT":
        reason = f"Allowed by approved policy v{v}: {said}."
        return Verdict("ACCEPT", reason, "policy", **common)
    if gated.action == "BLOCK":
        reason = f"Blocked by approved policy v{v}: {said}, below what it accepts."
        return Verdict("BLOCK", reason, "policy", **common)
    reason = f"Needs a person (approved policy v{v}): {said}, between its thresholds."
    return Verdict("ESCALATE", reason, "policy", **common)


def policy_models(client: Client, name: str = QUESTION) -> list[str]:
    """The served models that have an approved policy for the question."""
    question = client.snapshot.questions.get(name)
    if question is None:
        return []
    return sorted(m for (q, m) in client.snapshot.policies if q == question["question_id"])


def explain(
    client: Client,
    *,
    name: str = QUESTION,
    model: str | None = None,
    probability: float | None = None,
) -> str:
    """The active policy for a question in plain words, and, given a probability, what it
    would do with it and why. Read-only: it asks nothing and logs nothing."""
    snap = client.snapshot
    question = snap.questions.get(name)
    if question is None:
        return f"trimset has no question called {name!r} in this project yet."
    qid, definition = question["question_id"], question["definition"]
    found = policy_models(client, name)
    if model is None and len(found) == 1:
        model = found[0]
    if model is None or (qid, model) not in snap.policies:
        if found:
            return f"There are approved policies for {name} on: {', '.join(found)}. Name one."
        return (
            f"There is no approved policy for {name} yet, so every call is sent to a person. "
            "Compile one, approve it and activate it (trimset policy compile, approve, activate)."
        )
    entry = snap.policies[(qid, model)]
    policy = entry["policy"]
    lines = [
        f"Question: {definition['instructions']}",
        f"Approved policy v{policy['version']} for {name} on {model} "
        f"(calibration: {policy['calibration']['method']}).",
        "Rules, checked in order, on the calibrated probability that the call is safe:",
    ]
    lines += [f"  {describe_rule(rule)}" for rule in policy["rules"]]
    lines.append(f"  otherwise {policy['fallback_action']}")
    guarantee = policy["guarantee"]
    if guarantee["status"] == "certified":
        lines.append(f"Guarantee ({entry['guarantee_status']}): {guarantee['statement']}")
    else:
        lines.append(
            f"No guarantee: {guarantee.get('reason')}. {guarantee.get('detail', '')}".rstrip()
        )
    audit = float(policy.get("audit_fraction") or 0)
    if audit > 0:
        lines.append(f"{audit:.0%} of calls go to a person whatever the model says (audit).")
    if probability is not None:
        applied = apply(policy, definition, [probability])
        lines.append(
            f"For a model probability of {probability:.2f}: calibrated {applied.scalar:.3f}, "
            f"so the policy says {applied.action}."
        )
    return "\n".join(lines)

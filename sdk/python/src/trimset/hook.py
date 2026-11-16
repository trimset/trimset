"""A Claude Code `PreToolUse` hook (TV.1, ADR 0014): before a tool call runs, ask a decision
model whether it is safe to run without a human, and let the project's approved policy
decide what Claude Code does with it.

    trimset hook --model jev-latest            # Claude Code runs this, with the call on stdin

The policy's answer becomes Claude Code's permission decision:

    ESCALATE  ->  ask    the user is prompted, and sees why
    BLOCK     ->  deny   the call doesn't run, and Claude is told why
    ACCEPT    ->  nothing: Claude Code carries on as it would without trimset
                  (with --grant: allow, so the call runs without a prompt)

By default the hook can only make Claude Code stricter. Letting a call through without a
prompt is a decision to make once there is an approved guarantee behind it, so it is
opt-in; and even then Claude Code's own deny and ask rules still apply.

A hook that fails must not let a call through. Claude Code lets a call proceed when a hook
exits with an error or prints something that isn't JSON, so this one never does: every
path ends in valid JSON on standard output, or deliberate silence, and exit status 0.
When trimset can't be reached or isn't configured, the answer is `ask`.

It records no outcomes. Claude Code doesn't say whether a user approved or denied an `ask`,
so a hook could only ever see the approvals, and labels that are all "safe" would teach the
policy that nothing is dangerous. Outcomes come from people (the review queue, `trimset`
or the MCP server's `outcome` tool for incidents).
"""

import contextlib
import json
import logging
import sys
from typing import IO, Any

from trimset.agent import Verdict, decide
from trimset.mcp import AgentConfig, make_client

log = logging.getLogger("trimset.hook")
DENIED = " Don't retry it as it stands; ask the user before doing anything like it."


def permission_output(verdict: Verdict, grant: bool) -> dict[str, Any] | None:
    """The JSON Claude Code expects from a PreToolUse hook, or None to say nothing."""
    if verdict.action == "ACCEPT":
        if not grant:
            return None
        decision = "allow"
    else:
        decision = verdict.claude_code  # ask or deny
    reason = f"trimset: {verdict.reason}"
    if verdict.decision_id:
        reason += f" (decision {verdict.decision_id})"
    if decision == "deny":
        reason += DENIED
    return _envelope(decision, reason)


def _envelope(decision: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def _ask(reason: str) -> dict[str, Any]:
    """Asking is the answer whenever trimset can't tell: never silence, never allow."""
    return _envelope("ask", f"trimset: Needs a person: {reason}.")


def hook_output(
    raw: str, *, model: str | None, grant: bool = False, timeout: float = 8.0
) -> dict[str, Any] | None:
    """What to print for one hook invocation. Never raises: whatever goes wrong, even in
    code this module doesn't control, is an ask."""
    try:
        return _decide(raw, model=model, grant=grant, timeout=timeout)
    except Exception as e:
        log.exception("hook failed")
        return _ask(f"the check failed ({type(e).__name__})")


def _decide(raw: str, *, model: str | None, grant: bool, timeout: float) -> dict[str, Any] | None:
    try:
        event = json.loads(raw)
    except ValueError:
        return _ask("the hook input was not JSON, so trimset can't tell what is about to run")
    if not isinstance(event, dict):
        return _ask("the hook input was not an object")
    if event.get("hook_event_name") not in (None, "PreToolUse"):
        return None  # not ours to decide
    try:
        config = AgentConfig.from_env(model=model)
    except Exception as e:  # a missing setting, or a config file that isn't what it should be
        log.warning("%s", e)
        return _ask(f"trimset is not configured ({e})")
    state = {"tool": event.get("tool_name"), "input": event.get("tool_input")}
    client = make_client(config)
    try:
        try:
            client.refresh()  # the only look at trimset's policies that may wait
        except Exception as e:
            log.warning("can't refresh policies (%s); using the last good ones", type(e).__name__)
        verdict = decide(
            client, state, model=config.model, name=config.name,
            instructions=config.instructions, timeout=timeout,
        )  # fmt: skip
        client.flush(2.0)  # the action report, before this process exits
    finally:
        with contextlib.suppress(Exception):
            client.close(0.5)
    if verdict.action == "ACCEPT" and not grant:
        log.info("allowed by policy, not granting a prompt-free run (use --grant)")
    return permission_output(verdict, grant)


def run(
    model: str | None,
    grant: bool = False,
    timeout: float = 8.0,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="trimset hook: %(message)s")
    try:
        raw = (stdin or sys.stdin).read()
    except Exception:  # an unreadable input is still answered
        raw = ""
    out = hook_output(raw, model=model, grant=grant, timeout=timeout)
    if out is not None:
        with contextlib.suppress(OSError):  # a closed pipe: nothing more can be said
            (stdout or sys.stdout).write(json.dumps(out) + "\n")
    return 0

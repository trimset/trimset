#!/usr/bin/env python3
"""Formats what `trimset hook` and `trimset mcp` print, for the agent-gate demo.

    show.py hook <label>   one hook reply (or silence) on stdin
    show.py mcp            the replies of a `trimset mcp` session on stdin (ids 1 to 4)
    show.py decision-id    the decision id in the gate reply (id 3)
    show.py outcome        the reply to an outcome call (id 2)

Anything unexpected exits non-zero, so the demo stops instead of printing around it.
"""

import json
import sys
from typing import Any


def fail(message: str) -> None:
    sys.exit(f"show.py: {message}")


def indent(text: str, pad: str = "         ") -> str:
    return "\n".join(pad + line for line in text.splitlines())


def hook(label: str) -> None:
    raw = sys.stdin.read().strip()
    if not raw:  # the hook said nothing: Claude Code carries on as it would without trimset
        print(f"  {'silent':<7}{label}")
        print(indent("trimset has no objection; Claude Code decides as it would without it"))
        return
    try:
        out = json.loads(raw)["hookSpecificOutput"]
    except (ValueError, KeyError, TypeError):
        fail(f"the hook printed something Claude Code can't read: {raw[:200]!r}")
    if out.get("hookEventName") != "PreToolUse" or out.get("permissionDecision") not in (
        "allow",
        "ask",
        "deny",
    ):
        fail(f"not a PreToolUse permission decision: {out!r}")
    print(f"  {out['permissionDecision']:<7}{label}")
    print(indent(out["permissionDecisionReason"]))


def replies() -> dict[Any, dict[str, Any]]:
    found: dict[Any, dict[str, Any]] = {}
    for line in sys.stdin:
        if line.strip():
            reply = json.loads(line)
            found[reply.get("id")] = reply
    return found


def result(by_id: dict[Any, dict[str, Any]], mid: int) -> dict[str, Any]:
    reply = by_id.get(mid)
    if reply is None or "result" not in reply:
        fail(f"no result for request {mid}: {reply!r}")
    return reply["result"]  # type: ignore[index,no-any-return]


def text(res: dict[str, Any]) -> str:
    return res["content"][0]["text"]  # type: ignore[no-any-return]


def mcp() -> None:
    by_id = replies()
    server = result(by_id, 1)
    print(f"  initialize   {server['serverInfo']['name']}, protocol {server['protocolVersion']}")
    tools = [t["name"] for t in result(by_id, 2)["tools"]]
    print(f"  tools/list   {', '.join(tools)}")
    if tools != ["gate", "outcome", "explain"]:
        fail(f"expected gate, outcome and explain, got {tools}")
    print("  gate         " + text(result(by_id, 3)))
    print("  explain")
    print(indent(text(result(by_id, 4)), "    "))


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "hook" and len(sys.argv) == 3:
        hook(sys.argv[2])
    elif mode == "mcp":
        mcp()
    elif mode == "decision-id":
        decision_id = result(replies(), 3)["structuredContent"].get("decision_id")
        if not decision_id:
            fail("the gate reply has no decision id")
        print(decision_id)
    elif mode == "outcome":
        print("  outcome      " + text(result(replies(), 2)))
    else:
        fail(__doc__ or "bad arguments")


if __name__ == "__main__":
    main()

"""A Model Context Protocol server for trimset (TV.1, ADR 0014), over stdio.

    trimset mcp --model jev-latest        # an MCP client starts this and talks JSON-RPC to it

Three tools. `gate` asks trimset whether a tool call is safe to run without a human, and
answers ACCEPT, ESCALATE or BLOCK from the approved policy; `outcome` reports what
happened to an earlier decision; `explain` says what the active policy is and why it
decides as it does. It is a thin layer over the SDK, so it needs no new server endpoints:
the key is an ingest key, like any app's.

An agent must not be able to talk itself past its own gate, so `gate` asks the decision
model about the call itself and takes no probability from the agent, unless the operator
starts the server with --allow-probability. Reports of what happened are always recorded
as `downstream` outcomes, never as a person's review. When anything is missing or broken
the answer is ESCALATE, never ACCEPT.

The protocol is small enough to speak directly (newline-delimited JSON-RPC 2.0), which
keeps the SDK's one dependency its only one. Nothing but protocol messages goes to
standard output.
"""

import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from trimset import __version__
from trimset.agent import INSTRUCTIONS, QUESTION, Verdict, decide, explain, policy_models
from trimset.client import Client

log = logging.getLogger("trimset.mcp")
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


@dataclass(frozen=True)
class AgentConfig:
    url: str
    key: str
    model: str  # the decision model's name, as the app would send it: jev-latest
    name: str = QUESTION
    instructions: str = INSTRUCTIONS
    timeout: float = 10.0

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] = os.environ, model: str | None = None
    ) -> "AgentConfig":
        from trimset.cli import load_config  # the saved URL and key, as the CLI uses them

        saved = load_config()
        url = env.get("TRIMSET_URL") or saved.get("url")
        key = env.get("TRIMSET_KEY") or saved.get("key")
        chosen = model or env.get("TRIMSET_AGENT_MODEL")
        if not url or not key:
            raise ValueError(
                "no server configured: set TRIMSET_URL and TRIMSET_KEY (an ingest key)"
            )
        if not chosen:
            raise ValueError(
                "no decision model named: --model or TRIMSET_AGENT_MODEL, e.g. jev-latest"
            )
        return cls(
            url, key, chosen,
            name=env.get("TRIMSET_AGENT_QUESTION", QUESTION),
            instructions=env.get("TRIMSET_AGENT_INSTRUCTIONS", INSTRUCTIONS),
            timeout=float(env.get("TRIMSET_AGENT_TIMEOUT_S", "10")),
        )  # fmt: skip


def make_client(config: AgentConfig) -> Client:
    """The SDK client an agent gate uses: ESCALATE is its default for this question, and
    the last good policies are kept on disk so a short-lived process starts with them."""
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "trimset"
    return Client(
        config.url, config.key, defaults={config.name: "ESCALATE"},
        cache_path=cache / "agent-bundle.json", poll_interval=30.0, timeout=5.0,
        max_policy_age=600.0,
    )  # fmt: skip


def _verdict_json(v: Verdict) -> dict[str, Any]:
    return {
        "action": v.action,
        "reason": v.reason,
        "source": v.source,
        "policy_version": v.policy_version,
        "probability": v.probability,
        "calibrated": v.calibrated,
        "decision_id": v.decision_id,
    }


class Server:
    def __init__(
        self, client: Client, config: AgentConfig, *, allow_probability: bool = False
    ) -> None:
        self.client, self.config, self.allow_probability = client, config, allow_probability
        self._tools: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "gate": self._gate,
            "outcome": self._outcome,
            "explain": self._explain,
        }

    # ------------------------------------------------------------ the tools

    def tool_list(self) -> list[dict[str, Any]]:
        gate_props: dict[str, Any] = {
            "state": {
                "description": "What is about to run: the tool and its input, e.g. "
                '{"tool": "Bash", "input": {"command": "rm -rf build"}}. It is sent to the '
                "decision model, not to the agent's own context.",
            }
        }
        if self.allow_probability:
            gate_props["probability"] = {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "The probability the call is safe, if you already asked a model.",
            }  # fmt: skip
            gate_props["model_served"] = {
                "type": "string",
                "description": "The model that gave it, as it names itself; needed when "
                "there is more than one approved policy.",
            }
        return [
            {
                "name": "gate",
                "description": (
                    "Ask whether a tool call is safe to run without a human. trimset asks its "
                    "decision model about it, then the project's approved policy answers "
                    "ACCEPT (run it), ESCALATE (ask a person first) or BLOCK. If there is no "
                    "approved policy, the answer is ESCALATE. Keep the decision_id."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": gate_props,
                    "required": [] if self.allow_probability else ["state"],
                },
                "annotations": {"title": "Is this safe to run?", "readOnlyHint": False,
                                "idempotentHint": False, "openWorldHint": True},
            },
            {
                "name": "outcome",
                "description": (
                    "Report what happened to an earlier decision, so the next refit learns "
                    "from it: pass its decision_id and whether the call turned out to be safe. "
                    "Use it for incidents on calls that ran. It is recorded as a downstream "
                    "outcome, never as a person's review."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "decision_id": {"type": "string", "description": "From gate."},
                        "safe": {"type": "boolean", "description": "Was the call safe?"},
                    },
                    "required": ["decision_id", "safe"],
                },
                "annotations": {"title": "Report an outcome", "readOnlyHint": False,
                                "idempotentHint": False},
            },
            {
                "name": "explain",
                "description": (
                    "Say what the active policy is and why it decides as it does: its rules "
                    "in order, its stated guarantee, and, given a probability, what it would "
                    "do with it. Read-only."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "probability": {"type": "number", "minimum": 0, "maximum": 1,
                                        "description": "A model probability to walk through."},
                        "model": {"type": "string", "description": "The served model, if several."},
                    },
                },
                "annotations": {"title": "Explain the policy", "readOnlyHint": True},
            },
        ]  # fmt: skip

    def _gate(self, args: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config
        probability = args.get("probability") if self.allow_probability else None
        if probability is not None:
            from trimset.agent import Asked, verdict_of
            from trimset.agent import question_definition as definition

            if isinstance(probability, bool) or not isinstance(probability, int | float):
                raise ValueError("probability must be a number")
            self._refresh()
            served = args.get("model_served")
            models = policy_models(self.client, cfg.name)
            if not served and len(models) == 1:
                served = models[0]
            if not isinstance(served, str) or not served:
                raise ValueError(
                    f"model_served is required (approved policies: {models or 'none'})"
                )
            gated = self.client.gate(
                cfg.name, p=float(probability), model_served=served,
                question_def=definition(cfg.name, cfg.instructions), default="ESCALATE",
            )  # fmt: skip
            verdict = verdict_of(gated, Asked(float(probability), served, None))
        else:
            if "state" not in args:
                raise ValueError("state is required: what is about to run")
            self._refresh()
            verdict = decide(
                self.client, args["state"], model=cfg.model, name=cfg.name,
                instructions=cfg.instructions, timeout=cfg.timeout,
            )  # fmt: skip
        self.client.flush(2.0)
        data = _verdict_json(verdict)
        return {
            "content": [{"type": "text", "text": f"{verdict.action}: {verdict.reason}"
                         + (f" decision_id={verdict.decision_id}" if verdict.decision_id else "")}],
            "structuredContent": data,
            "isError": False,
        }  # fmt: skip

    def _outcome(self, args: dict[str, Any]) -> dict[str, Any]:
        decision_id, safe = args.get("decision_id"), args.get("safe")
        if not isinstance(decision_id, str) or not decision_id or not isinstance(safe, bool):
            raise ValueError("decision_id (a string) and safe (true or false) are required")
        self.client.outcome(decision_id=decision_id, label=safe, source="downstream")
        sent = self.client.flush(5.0)
        text = (
            "recorded"
            if sent
            else "queued: trimset could not be reached just now, and it will retry"
        )
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def _explain(self, args: dict[str, Any]) -> dict[str, Any]:
        self._refresh()
        p = args.get("probability")
        if p is not None and (
            isinstance(p, bool) or not isinstance(p, int | float) or not 0 <= p <= 1
        ):
            raise ValueError("probability must be a number between 0 and 1")
        text = explain(  # by the model that answers, not the alias asked for
            self.client, name=self.config.name, model=args.get("model"),
            probability=None if p is None else float(p),
        )  # fmt: skip
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def _refresh(self) -> None:
        """A fresh look at the policies, if trimset can be reached; else the last good ones."""
        try:
            self.client.refresh()
        except Exception as e:
            log.warning("can't refresh policies (%s); using the last good ones", type(e).__name__)

    # ------------------------------------------------------------ the protocol

    def handle(self, message: Any) -> Any:
        """One JSON-RPC message in, its response (or None for a notification) out."""
        if isinstance(message, list):
            replies = [r for m in message if (r := self.handle(m)) is not None]
            return replies or None
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or "method" not in message
        ):
            return _error(message.get("id") if isinstance(message, dict) else None,
                          INVALID_REQUEST, "not a JSON-RPC 2.0 request")  # fmt: skip
        mid, method = message.get("id"), message["method"]
        if "id" not in message:  # a notification: initialized, cancelled, ...
            return None
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(mid, INVALID_PARAMS, "params must be an object")
        try:
            if method == "initialize":
                return _result(mid, self._initialize(params))
            if method == "ping":
                return _result(mid, {})
            if method == "tools/list":
                return _result(mid, {"tools": self.tool_list()})
            if method == "tools/call":
                return self._call(mid, params)
        except Exception as e:  # a bug here must not end the session
            log.exception("failed on %s", method)
            return _error(mid, -32603, f"internal error ({type(e).__name__})")
        return _error(mid, METHOD_NOT_FOUND, f"no such method: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        wanted = params.get("protocolVersion")
        return {
            "protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "trimset", "title": "trimset", "version": __version__},
            "instructions": (
                "Before running a risky tool call, call gate with what is about to run. "
                "ACCEPT means go ahead, ESCALATE means ask the user first, BLOCK means "
                "don't. Report incidents with outcome."
            ),
        }

    def _call(self, mid: Any, params: dict[str, Any]) -> dict[str, Any]:
        name, args = params.get("name"), params.get("arguments")
        if args is None:
            args = {}
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _error(mid, INVALID_PARAMS, f"unknown tool: {name!r}")
        if not isinstance(args, dict):
            return _error(mid, INVALID_PARAMS, "arguments must be an object")
        try:
            return _result(mid, tool(args))
        except ValueError as e:  # the caller's mistake: reported to the model, which can retry
            return _result(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:
            log.exception("tool %s failed", name)
            text = f"{name} failed ({type(e).__name__}); treat this call as needing a person"
            return _result(mid, {"content": [{"type": "text", "text": text}], "isError": True})

    def serve(self, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                reply: Any = _error(None, PARSE_ERROR, "invalid JSON")
            else:
                reply = self.handle(message)
            if reply is not None:
                stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
                stdout.flush()


def _result(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def run(model: str | None, allow_probability: bool = False) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="trimset mcp: %(message)s")
    try:
        config = AgentConfig.from_env(model=model)
    except ValueError as e:
        print(f"trimset mcp: {e}", file=sys.stderr)
        return 1
    client = make_client(config)
    try:
        Server(client, config, allow_probability=allow_probability).serve()
    except KeyboardInterrupt:
        pass
    finally:
        client.close(2.0)
    return 0

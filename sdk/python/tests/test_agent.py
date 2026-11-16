"""Decisions for agents (TV.1): asking the model, gating its answer, failing safe, and the
MCP server over it. Against a fake trimset server that serves the policy bundle, answers
the proxy call and records what the SDK reports."""

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from trimset import Client, GateResult, agent
from trimset.agent import (
    AgentError,
    Asked,
    ask_model,
    decide,
    explain,
    question_definition,
    shorten,
    verdict_of,
)
from trimset.hashing import fingerprint, question_id
from trimset.mcp import AgentConfig, Server

EXAMPLES = Path(__file__).parents[3] / "docs" / "03-api" / "examples"
EXAMPLE = json.loads((EXAMPLES / "bundle.json").read_text())
MODEL = "jev-1.14.0"
DEFINITION = question_definition()
QID = question_id(DEFINITION)


def _bundle(*, policy: bool = True, instructions: str | None = None) -> dict[str, Any]:
    """A bundle with the agent question, and an approved policy for MODEL: ACCEPT from
    0.85 up, BLOCK below 0.30, a person in between."""
    definition = copy.deepcopy(DEFINITION)
    if instructions is not None:
        definition["instructions"] = instructions
    qid = question_id(definition)
    body = copy.deepcopy(EXAMPLE["policies"][0]["policy"]) | {
        "question_id": qid,
        "question_name": agent.QUESTION,
        "type": "noul",
        "version": 7,
        "model_served": MODEL,
        "calibration": {"method": "identity"},
        "audit_fraction": 0,
        "rules": [
            {"id": "accept", "action": "ACCEPT", "scalar": "p", "op": "ge", "threshold": 0.85},
            {"id": "block", "action": "BLOCK", "scalar": "p", "op": "lt", "threshold": 0.30},
        ],
    }
    body["fingerprint"] = fingerprint(body)
    return EXAMPLE | {
        "questions": {agent.QUESTION: {"question_id": qid, "definition": definition}},
        "policies": [{"policy": body, "guarantee_status": "certified"}] if policy else [],
    }


class Trimset:
    """The parts of trimset an agent gate talks to."""

    def __init__(self, bundle: dict[str, Any], p: float | Callable[[Any], float] = 0.97) -> None:
        self.bundle, self.p, self.served = bundle, p, MODEL
        self.status = 200
        self.asked: list[dict[str, Any]] = []
        self.posts: list[tuple[str, Any]] = []
        self.redirect = False
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._send(200, server.bundle, {"ETag": f'"{server.bundle["etag"]}"'})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/v1/systemone":
                    server.asked.append(body | {"_key": self.headers.get("X-Trimset-Key")})
                    if server.redirect:
                        self.send_response(302)
                        self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if server.status != 200:
                        return self._send(server.status, {"title": "no"})
                    name = next(iter(body["questions"]))
                    p = server.p(body["state"]) if callable(server.p) else server.p
                    answer = {"type": "noul", "noul": p}
                    return self._send(
                        200,
                        {"model": server.served, "answers": {name: answer}},
                        {"X-Trimset-Decision-Id": str(uuid.uuid4())},
                    )
                server.posts.append((self.path, body))
                self._send(200, {"stored": 1, "duplicates": 0, "rejected": 0, "matched": 1})

            def log_message(self, *args: Any) -> None:
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"

    def reports(self) -> list[dict[str, Any]]:
        return [
            r for path, b in self.posts if path == "/v1/decisions:actions" for r in b["actions"]
        ]

    def outcomes(self) -> list[dict[str, Any]]:
        return [o for path, b in self.posts if path == "/v1/outcomes" for o in b["outcomes"]]


@pytest.fixture
def trimset() -> Iterator[Trimset]:
    fake = Trimset(_bundle())
    yield fake
    fake.http.shutdown()


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read, or write, the developer's own config or cache."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name in ("TRIMSET_URL", "TRIMSET_KEY", "TRIMSET_AGENT_MODEL"):
        monkeypatch.delenv(name, raising=False)


def _client(fake: Trimset, **options: Any) -> Client:
    options.setdefault("defaults", {agent.QUESTION: "ESCALATE"})
    client = Client(
        fake.url, "tsi_test", poll_interval=3600, timeout=2, flush_interval=0.05, **options
    )
    with contextlib.suppress(Exception):  # some tests are of an unreachable trimset
        client.refresh()
    return client


# ------------------------------------------------------------------ deciding


@pytest.mark.req("FR-35")
@pytest.mark.parametrize(
    "p,action,hook,words",
    [
        (0.97, "ACCEPT", "allow", "Allowed by approved policy v7"),
        (0.50, "ESCALATE", "ask", "Needs a person (approved policy v7)"),
        (0.10, "BLOCK", "deny", "Blocked by approved policy v7"),
    ],
)
def test_the_policy_turns_the_models_answer_into_run_ask_or_block(
    trimset: Trimset, p: float, action: str, hook: str, words: str
) -> None:
    trimset.p = p
    client = _client(trimset)
    v = decide(client, {"tool": "Bash", "input": {"command": "ls"}}, model="jev-latest")
    assert (v.action, v.claude_code, v.source, v.policy_version) == (action, hook, "policy", 7)
    assert v.reason.startswith(words) and f"{p:.0%} safe" in v.reason
    assert v.probability == p and v.decision_id
    # The model was asked through the proxy with the key, once, and the call itself as state.
    (asked,) = trimset.asked
    assert asked["_key"] == "tsi_test" and asked["model"] == "jev-latest"
    assert asked["state"] == {"tool": "Bash", "input": {"command": "ls"}}
    assert asked["questions"] == {
        "safe_to_run": {
            "type": "noul",
            "instructions": "Is this tool call safe to run without a human?",
        }
    }
    # What was done is reported against the proxy's decision id, so it is logged once.
    assert client.flush(5)
    (report,) = trimset.reports()
    assert report["decision_id"] == v.decision_id and report["action_taken"] == action
    assert report["action_source"] == "policy" and report["policy_version"] == 7
    client.close()


@pytest.mark.req("NFR-07")
def test_anything_that_goes_wrong_asks_a_person_and_never_allows(trimset: Trimset) -> None:
    # The model errors.
    trimset.status = 500
    v = decide(_client(trimset), {"tool": "Bash"}, model="jev-latest")
    assert (v.action, v.claude_code, v.source, v.why_default) == (
        "ESCALATE", "ask", "default", "model_unreachable",
    )  # fmt: skip
    assert "could not be reached" in v.reason
    # The model answers with nonsense, or a version the policy wasn't compiled for.
    trimset.status = 200
    for p in (1.5, -0.1, "high", True, None):
        trimset.p = p  # type: ignore[assignment]
        assert decide(_client(trimset), {}, model="m").action == "ESCALATE"
    trimset.p = 0.99
    trimset.served = "jev-9.9.9"
    v = decide(_client(trimset), {}, model="jev-latest")
    assert (v.action, v.why_default) == ("ESCALATE", "model_mismatch")
    assert "version the policy was not compiled for" in v.reason
    # No approved policy yet.
    trimset.served = MODEL
    trimset.bundle = _bundle(policy=False)
    v = decide(_client(trimset), {}, model="jev-latest")
    assert (v.action, v.why_default) == ("ESCALATE", "no_policy")
    assert "no approved policy" in v.reason
    # trimset itself is gone.
    trimset.http.shutdown()
    trimset.http.server_close()
    gone = Client(trimset.url, "tsi_test", poll_interval=3600, timeout=0.5)
    v = decide(gone, {}, model="jev-latest", timeout=0.5)
    assert (v.action, v.source) == ("ESCALATE", "default")


@pytest.mark.req("FR-35")
def test_a_default_of_accept_is_never_honoured() -> None:
    for gated in (
        GateResult(action="ACCEPT", source="default", reason="no_policy", decision_id="d"),
        GateResult(action="BLOCK", source="default", reason="model_mismatch", decision_id="d"),
        GateResult(action="ACCEPT", source="default", reason=None, decision_id="d"),
    ):
        v = verdict_of(gated, Asked(0.99, "m", "d"))
        assert (v.action, v.claude_code) == ("ESCALATE", "ask")
    audited = GateResult(action="ESCALATE", source="audit", policy_version=7, decision_id="d")
    v = verdict_of(audited, Asked(0.99, "m", "d"))
    assert (v.action, v.source) == ("ESCALATE", "audit") and "audit" in v.reason


def test_a_reworded_question_is_a_different_question(trimset: Trimset) -> None:
    trimset.p = 0.99
    v = decide(
        _client(trimset), {}, model="jev-latest", instructions="Is it fine to proceed?"
    )  # the policy was compiled for the original wording
    assert (v.action, v.why_default) == ("ESCALATE", "question_changed")


def test_long_strings_are_cut_before_they_go_to_the_model() -> None:
    big = "x" * 10_000
    out = shorten({"tool": "Write", "input": {"content": big, "path": "a.txt", "n": 3}, "l": [big]})
    assert out["input"]["path"] == "a.txt" and out["input"]["n"] == 3
    assert len(out["input"]["content"]) < 4100 and out["input"]["content"].endswith("[6000 more]")
    assert out["l"][0].endswith("[6000 more]")
    assert shorten("short") == "short" and shorten(None) is None


def test_asking_the_model_checks_what_it_is_given(trimset: Trimset) -> None:
    with pytest.raises(AgentError, match="http"):
        ask_model("file:///etc/passwd", "k", "m", {})
    trimset.redirect = True  # a redirect would carry the key elsewhere
    with pytest.raises(AgentError, match="302"):
        ask_model(trimset.url, "tsi_test", "m", {})
    with pytest.raises(AgentError, match="can't reach"):
        ask_model("http://127.0.0.1:1", "k", "m", {}, timeout=0.5)


# ------------------------------------------------------------------ explaining


def test_explain_says_what_the_policy_is_and_what_it_would_do(trimset: Trimset) -> None:
    client = _client(trimset)
    text = explain(client, probability=0.5)
    assert "Is this tool call safe to run without a human?" in text
    assert "Approved policy v7 for safe_to_run on jev-1.14.0" in text
    assert text.index("ACCEPT when p >= 0.8500") < text.index("BLOCK when p < 0.3000")
    assert "otherwise ESCALATE" in text and "Guarantee (certified)" in text
    assert "For a model probability of 0.50: calibrated 0.500, so the policy says ESCALATE." in text
    assert "says ACCEPT" in explain(client, probability=0.9)
    assert "says BLOCK" in explain(client, model=MODEL, probability=0.01)
    assert "No policy" not in text and "audit" not in text  # audit_fraction is 0 here


def test_explain_when_there_is_nothing_to_explain(trimset: Trimset) -> None:
    assert "no question called 'nope'" in explain(_client(trimset), name="nope")
    trimset.bundle = _bundle(policy=False)
    text = explain(_client(trimset))
    assert "no approved policy" in text and "every call is sent to a person" in text
    trimset.bundle = _bundle()
    assert "approved policies for safe_to_run on: jev-1.14.0. Name one." in explain(
        _client(trimset), model="other"
    )


# ------------------------------------------------------------------ the MCP server


def _server(trimset: Trimset, **options: Any) -> Server:
    config = AgentConfig(trimset.url, "tsi_test", "jev-latest", timeout=2.0)
    return Server(_client(trimset), config, **options)


def _call(server: Server, name: str, arguments: dict[str, Any], mid: int = 1) -> dict[str, Any]:
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": mid,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert reply["id"] == mid and "result" in reply, reply
    return reply["result"]  # type: ignore[no-any-return]


def test_the_handshake_negotiates_a_version_and_lists_three_tools(trimset: Trimset) -> None:
    s = _server(trimset)
    init = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-03-26", "capabilities": {}}})  # fmt: skip
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert (
        init["result"]["serverInfo"]["name"] == "trimset"
        and "tools" in init["result"]["capabilities"]
    )
    newer = s.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                      "params": {"protocolVersion": "2031-01-01"}})  # fmt: skip
    assert newer["result"]["protocolVersion"] == "2025-06-18"  # ours: the client may go on or leave
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert s.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"] == {}
    tools = s.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["gate", "outcome", "explain"]
    by_name = {t["name"]: t for t in tools}
    assert by_name["explain"]["annotations"]["readOnlyHint"] is True
    assert by_name["gate"]["inputSchema"]["required"] == ["state"]
    assert "probability" not in by_name["gate"]["inputSchema"]["properties"]  # an agent can't vouch


def test_protocol_errors_are_answered_and_never_end_the_session(trimset: Trimset) -> None:
    s = _server(trimset)
    assert s.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"] == -32601
    assert s.handle({"id": 2, "method": "ping"})["error"]["code"] == -32600  # no "jsonrpc"
    assert s.handle("hello")["error"]["code"] == -32600
    assert s.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "rm_rf"}})["error"]["code"] == -32602  # fmt: skip
    bad_arguments = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "gate", "arguments": []}}  # fmt: skip
    assert s.handle(bad_arguments)["error"]["code"] == -32602
    batch = s.handle([{"jsonrpc": "2.0", "id": 5, "method": "ping"},
                      {"jsonrpc": "2.0", "method": "notifications/x"}])  # fmt: skip
    assert [r["id"] for r in batch] == [5]


@pytest.mark.req("FR-35")
def test_gate_asks_the_model_itself_and_returns_the_policys_verdict(trimset: Trimset) -> None:
    trimset.p = 0.2
    s = _server(trimset)
    result = _call(s, "gate", {"state": {"tool": "Bash", "input": {"command": "rm -rf /"}}})
    assert result["isError"] is False
    data = result["structuredContent"]
    assert (data["action"], data["policy_version"], data["probability"]) == ("BLOCK", 7, 0.2)
    assert result["content"][0]["text"].startswith("BLOCK: Blocked by approved policy v7")
    assert f"decision_id={data['decision_id']}" in result["content"][0]["text"]
    assert trimset.asked[0]["state"]["input"]["command"] == "rm -rf /"
    assert [r["action_taken"] for r in trimset.reports()] == ["BLOCK"]  # flushed before it answered


@pytest.mark.req("FR-35")
def test_an_agent_cannot_vouch_for_itself(trimset: Trimset) -> None:
    trimset.p = 0.05  # the model thinks it is dangerous
    s = _server(trimset)
    result = _call(s, "gate", {"state": {"tool": "Bash"}, "probability": 0.99})  # a forged answer
    assert result["structuredContent"]["action"] == "BLOCK"  # the model's answer decided
    assert result["structuredContent"]["probability"] == 0.05
    # Only the operator can allow an agent's own probability.
    allowed = _server(trimset, allow_probability=True)
    forged = _call(allowed, "gate", {"probability": 0.99})
    assert (
        forged["structuredContent"]["action"] == "ACCEPT"
        and forged["structuredContent"]["decision_id"] is None
    )
    assert len(trimset.asked) == 1  # the second call didn't ask the model at all
    tools = allowed.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert "probability" in {t["name"]: t for t in tools}["gate"]["inputSchema"]["properties"]


def test_gate_needs_something_to_decide_about_and_fails_safe(trimset: Trimset) -> None:
    s = _server(trimset)
    missing = _call(s, "gate", {})
    assert missing["isError"] is True and "state is required" in missing["content"][0]["text"]
    trimset.status = 500
    broken = _call(s, "gate", {"state": {"tool": "Bash"}})
    assert broken["structuredContent"]["action"] == "ESCALATE" and broken["isError"] is False


def test_outcomes_are_recorded_as_downstream_and_checked(trimset: Trimset) -> None:
    s = _server(trimset)
    result = _call(
        s, "outcome", {"decision_id": "0193f0a2-7c4e-7b1a-9d2e-5f6a7b8c9d0e", "safe": False}
    )
    assert result["content"][0]["text"] == "recorded" and result["isError"] is False
    (outcome,) = trimset.outcomes()
    assert outcome["label"] is False and outcome["source"] == "downstream"  # never human_review
    assert outcome["decision_id"] == "0193f0a2-7c4e-7b1a-9d2e-5f6a7b8c9d0e"
    for bad in (
        {},
        {"decision_id": "d"},
        {"decision_id": "", "safe": True},
        {"decision_id": "d", "safe": "no"},
    ):
        assert _call(s, "outcome", bad)["isError"] is True
    assert len(trimset.outcomes()) == 1


def test_explain_is_a_tool_too(trimset: Trimset) -> None:
    s = _server(trimset)
    text = _call(s, "explain", {"probability": 0.9})["content"][0]["text"]
    assert "ACCEPT when p >= 0.8500" in text and "says ACCEPT" in text
    assert _call(s, "explain", {"probability": 7})["isError"] is True
    assert not trimset.posts  # read-only: nothing was logged or reported


def test_serve_reads_lines_and_writes_only_protocol_messages(trimset: Trimset) -> None:
    s = _server(trimset)
    out = io.StringIO()
    lines = [
        '{"jsonrpc": "2.0", "id": 1, "method": "ping"}',
        "",
        "this is not json",
        '{"jsonrpc": "2.0", "method": "notifications/initialized"}',
        '{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}',
    ]
    s.serve(io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700


MAIN = [
    sys.executable,
    "-c",
    "import sys; from trimset.cli import main; sys.exit(main(sys.argv[1:]))",
]


def _run(args: list[str], env: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the interpreter running these tests
        [*MAIN, *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_the_command_speaks_mcp_on_real_pipes_and_prints_nothing_else(
    trimset: Trimset, tmp_path: Path
) -> None:
    trimset.p = 0.97
    env = os.environ | {
        "TRIMSET_URL": trimset.url,
        "TRIMSET_KEY": "tsi_test",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    call = {"name": "gate", "arguments": {"state": {"tool": "Bash", "input": {"command": "ls"}}}}
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": call},
    ]
    done = _run(
        ["mcp", "--model", "jev-latest"], env, "".join(json.dumps(m) + "\n" for m in messages)
    )
    assert done.returncode == 0, done.stderr
    replies = [json.loads(line) for line in done.stdout.splitlines()]  # every line is JSON-RPC
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["structuredContent"]["action"] == "ACCEPT"


def test_the_command_says_what_is_missing_on_stderr(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TRIMSET_")}
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    out = _run(["mcp", "--model", "m"], env)
    assert out.returncode == 1 and out.stdout == "" and "no server configured" in out.stderr
    env |= {"TRIMSET_URL": "http://127.0.0.1:1", "TRIMSET_KEY": "k"}
    out = _run(["mcp"], env)
    assert out.returncode == 1 and out.stdout == "" and "no decision model named" in out.stderr

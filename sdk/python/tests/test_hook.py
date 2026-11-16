"""The Claude Code PreToolUse hook (TV.1): the policy's answer as a permission decision,
stricter-only by default, and never a way for a call to slip through when something fails.
Against the shared fake trimset server (conftest.py)."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from trimset.agent import Verdict
from trimset.cli import config_path, load_config
from trimset.client import Client
from trimset.hook import hook_output, permission_output, run

pytestmark = pytest.mark.usefixtures("isolated_config")
MAIN = [
    sys.executable,
    "-c",
    "import sys; from trimset.cli import main; sys.exit(main(sys.argv[1:]))",
]
CALL = {
    "session_id": "s1",
    "cwd": "/home/user/app",
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf build", "description": "clean"},
    "tool_use_id": "toolu_01",
}


@pytest.fixture
def configured(trimset: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("TRIMSET_URL", trimset.url)
    monkeypatch.setenv("TRIMSET_KEY", "tsi_test")
    return trimset


def _out(raw: dict[str, Any] | str, **options: Any) -> dict[str, Any] | None:
    return hook_output(
        raw if isinstance(raw, str) else json.dumps(raw), model="jev-latest", **options
    )


def _decision(out: dict[str, Any] | None) -> tuple[str, str]:
    assert out is not None
    h = out["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse"
    return h["permissionDecision"], h["permissionDecisionReason"]


@pytest.mark.req("FR-35")
def test_the_policy_becomes_a_permission_decision_and_can_only_tighten_by_default(
    configured: Any,
) -> None:
    configured.p = 0.5  # between the thresholds: a person decides
    decision, reason = _decision(_out(CALL))
    assert decision == "ask" and reason.startswith("trimset: Needs a person (approved policy v7)")
    assert "(decision " in reason

    configured.p = 0.1
    decision, reason = _decision(_out(CALL))
    assert decision == "deny" and "Blocked by approved policy v7" in reason
    assert reason.endswith("ask the user before doing anything like it.")  # Claude reads this one

    configured.p = 0.97  # safe: but by default trimset only ever tightens
    assert _out(CALL) is None
    decision, reason = _decision(_out(CALL, grant=True))
    assert decision == "allow" and "Allowed by approved policy v7" in reason
    # Each of the five calls was reported against the proxy's decision id.
    assert [r["action_taken"] for r in configured.reports()] == [
        "ESCALATE", "BLOCK", "ACCEPT", "ACCEPT",
    ]  # fmt: skip


def test_only_the_call_itself_is_sent_to_the_model(configured: Any) -> None:
    configured.p = 0.5
    big = "y" * 10_000  # a long file, but one the model can be shown in full
    _out(CALL | {"tool_name": "Write", "tool_input": {"file_path": "a.txt", "content": big}})
    (asked,) = configured.asked
    assert set(asked["state"]) == {"tool", "input"}  # not the cwd, the session or the transcript
    assert asked["state"]["tool"] == "Write" and asked["state"]["input"]["file_path"] == "a.txt"
    assert asked["state"]["input"]["content"] == big  # whole: what it vouches for is what runs
    assert asked["model"] == "jev-latest" and asked["_key"] == "tsi_test"


@pytest.mark.req("NFR-07")
def test_a_call_too_long_to_check_in_full_goes_to_a_person_and_is_never_sent(
    configured: Any,
) -> None:
    configured.p = 0.99  # the model would say safe, about the start of the command
    padded = "echo ok " + "# " * 15_000 + "; rm -rf ~"  # the dangerous part falls off the end
    call = CALL | {"tool_input": {"command": padded}}
    for grant in (False, True):  # --grant can't turn it into an allow
        decision, reason = _decision(_out(call, grant=grant))
        assert decision == "ask" and "too long to check in full" in reason
    nested = CALL | {"tool_input": {"edits": [{"new_string": "z" * 20_001}]}}
    assert _decision(_out(nested, grant=True))[0] == "ask"
    assert not configured.asked  # the model was never shown a cut version
    assert not configured.reports()
    at_limit = CALL | {"tool_input": {"command": "z" * 20_000}}
    assert _decision(_out(at_limit, grant=True))[0] == "allow"  # the limit itself is fine


@pytest.mark.req("NFR-07")
def test_nothing_that_fails_lets_a_call_through_even_when_granting(
    configured: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured.p = 0.99  # the model would say safe
    configured.status = 500  # ...but it errors
    decision, reason = _decision(_out(CALL, grant=True))
    assert decision == "ask" and "could not be reached" in reason

    configured.status = 200
    configured.served = "jev-9.9.9"  # a version the policy wasn't compiled for
    assert _decision(_out(CALL, grant=True))[0] == "ask"
    configured.served = "jev-1.14.0"

    configured.bundle = configured.bundle_of(policy=False)  # no approved policy
    decision, reason = _decision(_out(CALL, grant=True))
    assert decision == "ask" and "no approved policy" in reason

    configured.http.shutdown()  # trimset is gone
    configured.http.server_close()
    assert _decision(_out(CALL, grant=True, timeout=0.5))[0] == "ask"

    import trimset.hook as hook

    def explode(*a: Any, **k: Any) -> Verdict:
        raise RuntimeError("a bug")

    monkeypatch.setattr(hook, "decide", explode)  # and a bug in the hook itself
    assert _decision(_out(CALL, grant=True))[0] == "ask"


@pytest.mark.req("NFR-07")
def test_a_failure_anywhere_in_the_hook_is_an_ask_never_a_crash(
    configured: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook that raises exits non-zero, and Claude Code lets the call proceed."""
    import trimset.hook as hook

    configured.p = 0.99

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "make_client", boom)  # building the client
    decision, reason = _decision(_out(CALL, grant=True))
    assert decision == "ask" and "the check failed (RuntimeError)" in reason
    monkeypatch.undo()

    monkeypatch.setenv("TRIMSET_URL", configured.url)
    monkeypatch.setenv("TRIMSET_KEY", "tsi_test")
    monkeypatch.setattr(hook.AgentConfig, "from_env", boom)  # reading the settings
    decision, reason = _decision(_out(CALL, grant=True))
    assert decision == "ask" and "not configured (boom)" in reason
    monkeypatch.undo()

    monkeypatch.setenv("TRIMSET_URL", configured.url)
    monkeypatch.setenv("TRIMSET_KEY", "tsi_test")
    real_close = Client.close

    def close_then_fail(self: Client, timeout: float = 2.0) -> None:
        real_close(self, timeout)  # really stop it: a leaked client outlives this test
        raise RuntimeError("boom")

    monkeypatch.setattr(Client, "close", close_then_fail)  # shutting down: the decision stands
    assert _decision(_out(CALL, grant=True))[0] == "allow"


def test_a_config_file_that_is_not_an_object_is_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config_path()
    path.parent.mkdir(parents=True)
    for junk in ('["a", "b"]', "null", '"text"', "7", '{"url": 5, "key": ["x"]}', "{not json"):
        path.write_text(junk)
        assert load_config() == {}
        decision, reason = _decision(_out(CALL, grant=True))
        assert decision == "ask" and "trimset is not configured" in reason
    path.write_text('{"url": "http://x", "key": "k", "extra": 1}')
    assert load_config() == {"url": "http://x", "key": "k"}


def test_input_that_isnt_ours_is_handled(configured: Any) -> None:
    configured.p = 0.5
    assert _out(CALL | {"hook_event_name": "PostToolUse"}) is None  # not ours to decide
    assert not configured.asked
    for garbage in ("", "not json", "[1, 2]", "null", '"text"'):
        decision, reason = _decision(_out(garbage))
        assert decision == "ask" and reason.startswith("trimset: Needs a person")
    minimal = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}  # no tool_input
    assert _decision(_out(minimal))[0] == "ask"
    assert configured.asked[-1]["state"] == {"tool": "Bash", "input": None}


def test_an_unconfigured_hook_asks_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    decision, reason = _decision(_out(CALL))
    assert decision == "ask" and "trimset is not configured" in reason
    monkeypatch.setenv("TRIMSET_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("TRIMSET_KEY", "k")
    decision, reason = _decision(hook_output(json.dumps(CALL), model=None))
    assert decision == "ask" and "no decision model named" in reason


def test_the_output_has_exactly_the_shape_claude_code_reads() -> None:
    v = Verdict("BLOCK", "no.", "policy", decision_id="d1")
    out = permission_output(v, grant=False)
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "trimset: no. (decision d1) Don't retry it as it "
            "stands; ask the user before doing anything like it.",
        }
    }
    assert permission_output(Verdict("ACCEPT", "yes.", "policy"), grant=False) is None


def test_run_writes_one_json_line_or_nothing_and_always_exits_zero(configured: Any) -> None:
    configured.p = 0.5
    out = io.StringIO()
    assert run("jev-latest", stdin=io.StringIO(json.dumps(CALL)), stdout=out) == 0
    (line,) = out.getvalue().splitlines()
    assert json.loads(line)["hookSpecificOutput"]["permissionDecision"] == "ask"
    silent = io.StringIO()
    configured.p = 0.97
    assert run("jev-latest", stdin=io.StringIO(json.dumps(CALL)), stdout=silent) == 0
    assert silent.getvalue() == ""


def test_run_answers_even_when_the_hook_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trimset.hook as hook

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "_decide", boom)
    out = io.StringIO()
    assert run("jev-latest", stdin=io.StringIO(json.dumps(CALL)), stdout=out) == 0
    assert _decision(json.loads(out.getvalue()))[0] == "ask"

    class Unreadable(io.StringIO):
        def read(self, *a: Any) -> str:
            raise OSError("closed")

    out = io.StringIO()  # stdin that can't be read is answered too
    assert run("jev-latest", stdin=Unreadable(), stdout=out) == 0
    assert _decision(json.loads(out.getvalue()))[0] == "ask"


def test_the_command_prints_only_the_decision_on_real_pipes(
    configured: Any, tmp_path: Path
) -> None:
    env = os.environ | {
        "TRIMSET_URL": configured.url,
        "TRIMSET_KEY": "tsi_test",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }

    def invoke(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - the interpreter running these tests
            [*MAIN, "hook", "--model", "jev-latest", *args], input=json.dumps(CALL),
            capture_output=True, text=True, env=env, timeout=60, check=False,
        )  # fmt: skip

    configured.p = 0.1
    done = invoke()
    assert done.returncode == 0, done.stderr
    assert _decision(json.loads(done.stdout))[0] == "deny"
    configured.p = 0.97
    quiet = invoke()
    assert quiet.returncode == 0 and quiet.stdout == ""
    granted = invoke("--grant")
    assert _decision(json.loads(granted.stdout))[0] == "allow"

"""The CLI against a fake API on a local port: real HTTP, canned answers."""

import json
import stat
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from trimset import cli

Answer = tuple[int, Any] | Callable[[Any, dict[str, list[str]]], tuple[int, Any]]


class FakeAPI:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Answer] = {}
        self.seen: list[dict[str, Any]] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                url = urlsplit(self.path)
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                query = parse_qs(url.query)
                api.seen.append(
                    {
                        "method": self.command,
                        "path": url.path,
                        "query": query,
                        "body": body,
                        "auth": self.headers.get("authorization"),
                    }
                )
                route = api.routes.get(
                    (self.command, url.path), (404, {"title": "Not found", "status": 404})
                )
                status, out = route(body, query) if callable(route) else route
                data = json.dumps(out).encode()
                self.send_response(status)
                kind = "application/problem+json" if status >= 400 else "application/json"
                self.send_header("content-type", kind)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def bodies(self, method: str, path: str) -> list[Any]:
        return [s["body"] for s in self.seen if s["method"] == method and s["path"] == path]


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeAPI]:
    fake = FakeAPI()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("TRIMSET_URL", fake.url)
    monkeypatch.setenv("TRIMSET_KEY", "tsa_testkey0123456789abcdefghijklmn")
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    yield fake
    fake.server.shutdown()


def run(*argv: str) -> int:
    return cli.main(list(argv))


FIT = {
    "fit_id": "f1",
    "question_id": "q_" + "a" * 32,
    "model_served": "jev-1.14.0",
    "calibration": {"method": "platt", "a": -1.2, "b": 0.3},
    "selection": {"cv_folds": 5, "cv_log_loss": {"identity": 0.31, "platt": 0.27}},
    "metrics": {
        name: {
            "n": 400,
            "log_loss": ll,
            "brier": 0.08,
            "ece_equal_width": 0.06,
            "ece_equal_mass": 0.05,
            "bins": 15,
        }
        for name, ll in (("raw", 0.31), ("calibrated", 0.27))
    },
    "reliability": {
        "calibrated": [
            {
                "lo": 0.0,
                "hi": 0.1,
                "count": 300,
                "mean_predicted": 0.03,
                "observed": 0.02,
                "ci95": [0.01, 0.04],
            }
        ]
    },
}
METRICS = {
    "n": 1000,
    "automated": 0.9,
    "escalated": 0.1,
    "rules": {
        "accept": {
            "fired": 800,
            "wrong": 10,
            "unlabeled": 0,
            "rate": 0.0125,
            "ci95": [0.006, 0.023],
        }
    },
    "expected_cost_per_1000": None,
}
POLICY = {
    "policy": {
        "policy_id": "p1",
        "question_id": "q_" + "a" * 32,
        "question_name": "refund_fraud",
        "model_served": "jev-1.14.0",
        "version": 3,
        "fingerprint": "sha256:" + "0" * 64,
        "calibration": {"method": "platt"},
        "rules": [
            {"id": "accept", "action": "ACCEPT", "scalar": "p", "op": "lt", "threshold": 0.12}
        ],
        "option_actions": ["spam"],
        "fallback_action": "ESCALATE",
        "guarantee": {
            "status": "certified",
            "statement": "P(wrong | ACCEPT) <= 2%",
            "n": {"accept": 800},
        },
        "metrics": {"tune": METRICS, "eval": METRICS},
    },
    "state": "draft",
    "guarantee_status": "certified",
    "events": [],
}


def _job(result: Any) -> tuple[int, Any]:
    return 200, {"job_id": 1, "kind": "fit", "status": "succeeded", "result": result}


def test_config_is_saved_privately_and_shown_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("TRIMSET_URL", raising=False)
    monkeypatch.delenv("TRIMSET_KEY", raising=False)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "tsa_fromprompt0123456789abcdefghij")
    assert run("config", "set", "--url", "https://trimset.example.com/") == 0
    path = tmp_path / "trimset" / "config.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {
        "url": "https://trimset.example.com",
        "key": "tsa_fromprompt0123456789abcdefghij",
    }
    assert run("config", "show") == 0
    out = capsys.readouterr().out
    assert "tsa_from..." in out and "0123456789" not in out
    assert run("config", "set") == 1


@pytest.mark.req("FR-55")
def test_calibrate_waits_for_the_fit_and_prints_raw_against_calibrated(
    api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    api.routes[("POST", "/v1/admin/questions/refund_fraud/fits")] = (
        202,
        {"job_id": 1, "status": "queued"},
    )
    api.routes[("GET", "/v1/admin/jobs/1")] = _job({"fit_id": "f1"})
    api.routes[("GET", "/v1/admin/fits/f1")] = (200, FIT)
    assert run("calibrate", "refund_fraud", "--model", "jev-1.14.0") == 0
    out = capsys.readouterr().out
    assert "platt" in out and "log loss" in out and "Reliability" in out and "0.2700" in out
    assert api.bodies("POST", "/v1/admin/questions/refund_fraud/fits") == [
        {"model_served": "jev-1.14.0", "window_days": 90}
    ]
    assert all(s["auth"] == "Bearer tsa_testkey0123456789abcdefghijklmn" for s in api.seen)
    assert run("--json", "calibrate", "refund_fraud", "--model", "jev-1.14.0") == 0
    assert json.loads(capsys.readouterr().out)["fit_id"] == "f1"


def test_calibrate_reports_too_few_labels_and_failed_jobs(
    api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    api.routes[("POST", "/v1/admin/questions/q/fits")] = (202, {"job_id": 1, "status": "queued"})
    api.routes[("GET", "/v1/admin/jobs/1")] = _job(
        {"not_enough_labels": {"labeled": 120, "needed": 80}}
    )
    assert run("calibrate", "q", "--model", "m") == 2
    assert "not enough labels: 120 labeled, 80 more needed" in capsys.readouterr().err
    api.routes[("GET", "/v1/admin/jobs/1")] = (
        200,
        {"job_id": 1, "status": "failed", "error": "No fit"},
    )
    assert run("calibrate", "q", "--model", "m") == 1
    assert "failed: No fit" in capsys.readouterr().err


@pytest.mark.req("FR-26")
@pytest.mark.parametrize(
    ("question", "flags", "rules"),
    [
        (
            {"type": "noul", "settings": {}},
            ["--lo", "0.3", "--hi", "0.85"],
            [["accept", "ACCEPT", "p", "lt", 0.3], ["block", "BLOCK", "p", "ge", 0.85]],
        ),
        (
            {"type": "noul", "settings": {"actions": {"high_means": "accept"}}},
            ["--lo", "0.5", "--hi", "0.95"],
            [["accept", "ACCEPT", "p", "ge", 0.95], ["block", "BLOCK", "p", "lt", 0.5]],
        ),
        (
            {"type": "choice", "settings": {}},
            ["--t", "0.8"],
            [["select", "SELECT", "top", "ge", 0.8]],
        ),
        (
            {"type": "score", "settings": {"actions": {"accept_below_level": 2}}},
            ["--lo", "0.3"],
            [["accept", "ACCEPT", {"tail_ge": 2}, "lt", 0.3]],
        ),
    ],
)
def test_simulate_builds_rules_from_the_questions_actions(
    api: FakeAPI,
    capsys: pytest.CaptureFixture[str],
    question: Any,
    flags: list[str],
    rules: list[list[Any]],
) -> None:
    api.routes[("GET", "/v1/admin/questions/q")] = (200, question)
    row = {"rules": [], "metrics": METRICS}
    api.routes[("POST", "/v1/admin/questions/q/simulate")] = lambda body, _: (
        200,
        {"fit_id": "f1", "rows": [row | {"rules": body["candidates"][0]}]},
    )
    assert run("simulate", "q", "--model", "m", *flags) == 0
    sent = api.bodies("POST", "/v1/admin/questions/q/simulate")[0]["candidates"][0]
    assert [[r["id"], r["action"], r["scalar"], r["op"], r["threshold"]] for r in sent] == rules
    assert "90.0%" in capsys.readouterr().out


def test_simulate_without_thresholds_asks_for_a_grid_and_takes_costs(
    api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    api.routes[("GET", "/v1/admin/questions/q")] = (200, {"type": "noul", "settings": {}})
    rows = [{"rules": [], "metrics": METRICS | {"automated": a}} for a in (0.2, 0.7, 0.5)]
    api.routes[("POST", "/v1/admin/questions/q/simulate")] = (200, {"fit_id": "f1", "rows": rows})
    assert (
        run(
            "simulate",
            "q",
            "--model",
            "m",
            "--cost-escalation",
            "1",
            "--cost-wrong",
            "accept=20",
            "--top",
            "2",
        )
        == 0
    )
    body = api.bodies("POST", "/v1/admin/questions/q/simulate")[0]
    assert "candidates" not in body and body["costs"] == {
        "escalation": 1.0,
        "wrong": {"ACCEPT": 20.0},
    }
    out = capsys.readouterr().out
    assert out.index("70.0%") < out.index("50.0%") and "20.0%" not in out
    assert (
        run("simulate", "q", "--model", "m", "--cost-escalation", "1", "--cost-wrong", "nope") == 1
    )
    api.routes[("GET", "/v1/admin/questions/s")] = (200, {"type": "score", "settings": {}})
    assert run("simulate", "s", "--model", "m", "--hi", "0.5") == 1
    assert "needs a level" in capsys.readouterr().err


@pytest.mark.req("FR-22")
def test_policy_compile_sends_the_target_and_shows_the_policy(
    api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    api.routes[("POST", "/v1/admin/questions/refund_fraud/compile")] = (
        202,
        {"job_id": 1, "status": "queued"},
    )
    api.routes[("GET", "/v1/admin/jobs/1")] = _job({"policy_id": "p1", "version": 3})
    api.routes[("GET", "/v1/admin/policies/p1")] = (200, POLICY)
    args = [
        "policy",
        "compile",
        "refund_fraud",
        "--model",
        "jev-1.14.0",
        "--false-pass",
        "0.02",
        "--max-escalation",
        "0.15",
    ]
    assert run(*args) == 0
    sent = api.bodies("POST", "/v1/admin/questions/refund_fraud/compile")[0]
    assert sent == {
        "model_served": "jev-1.14.0",
        "target": {
            "kind": "risk",
            "metric": "false_pass",
            "alpha": 0.02,
            "delta": 0.05,
            "max_escalation": 0.15,
        },
    }
    out = capsys.readouterr().out
    assert "v3 draft" in out and "ACCEPT when p < 0.1200" in out and "guarantee (certified)" in out
    assert "options that BLOCK instead: spam" in out and "1.25%" in out

    assert run("policy", "compile", "q", "--model", "m") == 1  # no target
    assert (
        run(
            "policy", "compile", "q", "--model", "m", "--false-pass", "0.1", "--error-budget", "0.1"
        )
        == 1
    )
    api.routes[("POST", "/v1/admin/questions/q/compile")] = (202, {"job_id": 1, "status": "queued"})
    api.routes[("GET", "/v1/admin/jobs/1")] = _job(
        {"infeasible": {"message": "escalates 30% over the 10% cap"}}
    )
    assert (
        run(
            "policy",
            "compile",
            "q",
            "--model",
            "m",
            "--error-budget",
            "0.05",
            "--fit",
            "f1",
            "--audit-fraction",
            "0.01",
        )
        == 2
    )
    assert "infeasible: escalates 30%" in capsys.readouterr().err
    assert api.bodies("POST", "/v1/admin/questions/q/compile")[-1]["audit_fraction"] == 0.01
    assert (
        run(
            "policy",
            "compile",
            "q",
            "--model",
            "m",
            "--cost-escalation",
            "2",
            "--cost-wrong",
            "BLOCK=5",
        )
        == 2
    )
    assert api.bodies("POST", "/v1/admin/questions/q/compile")[-1]["target"] == {
        "kind": "cost",
        "costs": {"escalation": 2.0, "wrong": {"BLOCK": 5.0}},
    }


def test_policy_show_by_id_or_question_and_diff(
    api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    api.routes[("GET", "/v1/admin/policies/p1")] = (200, POLICY)
    api.routes[("GET", "/v1/admin/policies")] = lambda _, q: (
        200,
        {"items": [POLICY] if q["question"] == ["refund_fraud"] else []},
    )
    assert run("policy", "show", "p1") == 0
    assert run("policy", "show", "--question", "refund_fraud", "--model", "jev-1.14.0") == 0
    assert capsys.readouterr().out.count("v3 draft") == 2
    assert run("policy", "show", "--question", "other") == 2
    assert run("policy", "show") == 1
    no_guarantee = {
        **POLICY,
        "policy": {**POLICY["policy"], "guarantee": {"status": "none", "reason": "cost_target"}},
    }  # type: ignore[dict-item]
    api.routes[("GET", "/v1/admin/policies/p2")] = (200, no_guarantee)
    assert run("policy", "show", "p2") == 0
    assert "no guarantee: cost_target" in capsys.readouterr().out
    diff = {
        "policy_id": "p1",
        "against": None,
        "rules": [{"id": "accept", "before": None, "after": 0.12}],
        "metrics": {"before": None, "after": METRICS},
    }
    api.routes[("GET", "/v1/admin/policies/p1/diff")] = (200, diff)
    assert run("policy", "diff", "p1") == 0
    out = capsys.readouterr().out
    assert "against nothing (no active policy)" in out and "0.1200" in out


@pytest.mark.req("FR-14")
def test_coverage_warns_about_a_path_without_labels(
    api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = [
        {
            "model_served": "m",
            "action": "ACCEPT",
            "decisions": 900,
            "matured": 900,
            "labeled": 850,
            "audited_labeled": 9,
            "median_delay_hours": 50.0,
        },
        {
            "model_served": "m",
            "action": "BLOCK",
            "decisions": 100,
            "matured": 100,
            "labeled": 0,
            "median_delay_hours": None,
        },
    ]
    api.routes[("GET", "/v1/admin/questions/refund_fraud/coverage")] = (
        200,
        {"question_id": "q", "paths": paths},
    )
    assert run("coverage", "refund_fraud") == 0
    out = capsys.readouterr().out
    assert "850 (94.4%)" in out and "warning: BLOCK on m has almost no labels" in out


def test_decisions_tail_prints_the_latest(api: FakeAPI, capsys: pytest.CaptureFixture[str]) -> None:
    rows = [
        {
            "ts": "2026-11-11T10:00:02Z",
            "question_id": "q_x",
            "question_name": "fraud",
            "model_served": "m",
            "probabilities": [0.8312],
            "action_taken": "ESCALATE",
            "external_ref": "rf-2",
        },
        {
            "ts": "2026-11-11T10:00:01Z",
            "question_id": "q_y",
            "question_name": None,
            "model_served": "m",
            "probabilities": None,
            "action_taken": None,
            "external_ref": None,
        },
    ]
    api.routes[("GET", "/v1/admin/decisions")] = (200, {"items": rows})
    assert run("decisions", "tail", "--question", "fraud", "--once") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("2026-11-11T10:00:01Z  q_y  m  [refused]  -")
    assert "fraud  m  [0.831]  ESCALATE  rf-2" in lines[1]
    assert run("--json", "decisions", "tail", "--once") == 0
    assert [json.loads(x)["ts"] for x in capsys.readouterr().out.splitlines()] == [
        "2026-11-11T10:00:01Z",
        "2026-11-11T10:00:02Z",
    ]


def test_errors_say_what_went_wrong(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    problem = {
        "title": "Unknown or invalid settings",
        "status": 422,
        "detail": "bad",
        "errors": [{"pointer": "/settings/x", "message": "extra field"}],
    }
    api.routes[("GET", "/v1/admin/questions/q/coverage")] = (422, problem)
    assert run("coverage", "q") == 1
    err = capsys.readouterr().err
    assert "Unknown or invalid settings (422): bad" in err and "/settings/x extra field" in err
    assert run("--url", "http://127.0.0.1:1", "coverage", "q") == 1
    assert "can't reach" in capsys.readouterr().err
    assert run("--url", "file:///etc/passwd", "coverage", "q") == 1
    assert "http:// or https://" in capsys.readouterr().err
    monkeypatch.delenv("TRIMSET_KEY")
    assert run("coverage", "q") == 1
    assert "trimset config set" in capsys.readouterr().err


def test_a_job_that_never_finishes_is_left_to_check_later(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "JOB_TIMEOUT_S", -1.0)
    api.routes[("POST", "/v1/admin/questions/q/fits")] = (202, {"job_id": 7, "status": "queued"})
    assert run("calibrate", "q", "--model", "m") == 1
    assert "job 7 is still queued" in capsys.readouterr().err

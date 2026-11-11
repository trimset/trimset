"""The client: gate() (sdk.md §2-3) and the policy cache (§4), against a local fake server."""

import copy
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from trimset import Client, GateResult, policy
from trimset.client import Snapshot, build_snapshot
from trimset.hashing import fingerprint, question_id

EXAMPLES = Path(__file__).parents[3] / "docs" / "03-api" / "examples"
BUNDLE = json.loads((EXAMPLES / "bundle.json").read_text())
MODEL = "jev-1.14.0"
QID = BUNDLE["questions"]["refund_fraud"]["question_id"]


def _not_audited() -> str:
    """A decision id the example policy's 1% audit sampling leaves alone."""
    for i in range(100):
        did = f"019a6f4e-0000-7000-8000-{i:012d}"
        if not policy.audited(did, 0.01):
            return did
    raise AssertionError("unreachable")


DID = _not_audited()


class FakeServer:
    """Serves a bundle at /v1/policies with ETag and 304, and takes the SDK's POSTs:
    `posts` keeps (path, body) of each one answered 200; `statuses` lists the answers
    to give first (e.g. [503, 200])."""

    def __init__(self, bundle: dict[str, Any]) -> None:
        self.bundle, self.seen = bundle, []
        self.posts: list[tuple[str, Any]] = []
        self.statuses: list[int] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status = server.statuses.pop(0) if server.statuses else 200
                if status == 200:
                    server.posts.append((self.path, body))
                kind = next(iter(k for k in body if k in ("records", "actions", "outcomes")))
                data = json.dumps({"stored": len(body[kind]), "duplicates": 0, "rejected": 0})
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data.encode())

            def do_GET(self) -> None:
                server.seen.append({k.lower(): v for k, v in self.headers.items()})
                etag = f'"{server.bundle["etag"]}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                data = json.dumps(server.bundle).encode()
                self.send_response(200)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"


@pytest.fixture
def server() -> Iterator[FakeServer]:
    fake = FakeServer(copy.deepcopy(BUNDLE))
    yield fake
    fake.http.shutdown()


def _client(url: str = "http://127.0.0.1:1", **options: Any) -> Client:
    options.setdefault("poll_interval", 3600)
    return Client(url, "tsi_test", **options)


def _loaded(bundle: dict[str, Any] = BUNDLE, **options: Any) -> Client:
    """A client whose snapshot is `bundle`, without the network."""
    client = _client(**options)
    client.close()
    client._snapshot = build_snapshot(bundle, Snapshot(), time.time())
    return client


def _bundle(definition: dict[str, Any], **policy_fields: Any) -> dict[str, Any]:
    """A bundle with one question and one policy for MODEL, fingerprinted."""
    qid = question_id(definition)
    body = copy.deepcopy(BUNDLE["policies"][0]["policy"])
    body |= {"question_id": qid, "question_name": "q", "type": definition["type"]} | policy_fields
    body["fingerprint"] = fingerprint(body)
    return BUNDLE | {
        "questions": {"q": {"question_id": qid, "definition": definition}},
        "policies": [{"policy": body, "guarantee_status": "certified"}],
    }


@pytest.mark.req("FR-32")
@pytest.mark.req("FR-33")
def test_the_sdk_md_example_escalates_with_policy_v7() -> None:
    d = _loaded().gate("refund_fraud", p=0.83, model_served=MODEL, decision_id=DID)
    assert isinstance(d, GateResult)
    assert (d.action, d.source, d.reason, d.policy_version, d.audit) == (
        "ESCALATE", "policy", None, 7, False,
    )  # fmt: skip
    assert d.scalar == pytest.approx(0.6695, abs=1e-4)
    assert d.policy_fingerprint == BUNDLE["policies"][0]["policy"]["fingerprint"]
    assert d.decision_id == DID
    client = _loaded()
    assert (
        client.gate("refund_fraud", p=0.1, model_served=MODEL, decision_id=DID).action == "ACCEPT"
    )
    assert (
        client.gate("refund_fraud", p=0.99, model_served=MODEL, decision_id=DID).action == "BLOCK"
    )
    by_id = client.gate(QID, p=0.83, model_served=MODEL, decision_id=DID)
    assert by_id.action == "ESCALATE" and by_id.source == "policy"


def test_answers_in_either_wire_format() -> None:
    client = _loaded()
    systemone = {"type": "noul", "noul": 0.83}
    openai = {"type": "predicate", "probability": 0.83}
    for answer in (systemone, openai):
        d = client.gate("refund_fraud", answer=answer, model_served=MODEL, decision_id=DID)
        assert (d.action, d.source) == ("ESCALATE", "policy")
    refused = client.gate("refund_fraud", answer={"type": "refusal"}, model_served=MODEL)
    assert (refused.source, refused.reason) == ("default", "refusal")


@pytest.mark.req("FR-35")
def test_every_default_says_why() -> None:
    client = _loaded(defaults={"refund_fraud": "ACCEPT"})

    def reason(question: str = "refund_fraud", **kw: Any) -> str | None:
        kw.setdefault("model_served", MODEL)
        d = client.gate(question, **kw)
        assert d.source == "default" and d.decision_id
        return d.reason

    assert reason("nope", p=0.5) == "no_policy"
    assert reason(p=0.5, model_served=None) == "model_unknown"
    assert reason(p=0.5, model_served="jev-1.15.0") == "model_mismatch"
    assert reason(p=1.5) == "invalid_probabilities"
    assert reason(p=float("nan")) == "invalid_probabilities"
    assert reason(p=0.5, answer={"type": "noul", "noul": 0.5}) == "invalid_probabilities"
    assert reason() == "invalid_probabilities"
    assert reason(probabilities=[0.5, 0.5]) == "invalid_probabilities"  # noul takes p
    assert reason(answer={"type": "noul", "noul": "high"}) == "invalid_probabilities"
    assert reason(answer=object()) == "invalid_probabilities"
    changed = dict(BUNDLE["questions"]["refund_fraud"]["definition"], instructions="Is it fraud?")
    assert reason(p=0.5, question_def=changed) == "question_changed"
    same = BUNDLE["questions"]["refund_fraud"]["definition"]
    assert client.gate("refund_fraud", p=0.83, model_served=MODEL, question_def=same,
                       decision_id=DID).source == "policy"  # fmt: skip
    assert client.gate("nope", p=0.5).action == "ESCALATE"  # no default: the library's
    assert client.gate("refund_fraud", p=0.5, model_served="x").action == "ACCEPT"
    assert client.gate("refund_fraud", p=0.5, model_served="x", default="BLOCK").action == "BLOCK"

    pinned = _loaded(default_model=MODEL)
    assert pinned.gate("refund_fraud", p=0.83, decision_id=DID).source == "policy"
    stale = _loaded(max_policy_age=60)
    stale._snapshot = build_snapshot(BUNDLE, Snapshot(), time.time() - 120)
    assert stale.gate("refund_fraud", p=0.83, model_served=MODEL).reason == "stale_policy"


def test_the_library_default_warns_once_per_question(caplog: pytest.LogCaptureFixture) -> None:
    client = _loaded()
    with caplog.at_level(logging.WARNING, logger="trimset"):
        for _ in range(3):
            client.gate("nope", p=0.5)
    assert [r.getMessage() for r in caplog.records].count(
        "trimset: no default for nope; using ESCALATE"
    ) == 1
    with pytest.raises(ValueError, match="must be one of"):
        _client(defaults={"refund_fraud": "MAYBE"})


@pytest.mark.req("FR-37")
def test_audit_sampling_escalates_by_the_decision_ids_hash() -> None:
    full = _bundle(BUNDLE["questions"]["refund_fraud"]["definition"], audit_fraction=0.5)
    client = _loaded(full)
    results = [client.gate("q", p=0.01, model_served=MODEL) for _ in range(400)]
    audited = [d for d in results if d.source == "audit"]
    assert 150 < len(audited) < 250
    assert all(d.action == "ESCALATE" and d.audit and d.policy_version == 7 for d in audited)
    assert all(d.action == "ACCEPT" for d in results if d.source == "policy")
    did = audited[0].decision_id  # the same id is always audited
    assert client.gate("q", p=0.01, model_served=MODEL, decision_id=did.upper()).audit


def test_choice_selects_the_top_option_unless_it_is_blocked() -> None:
    definition = {
        "schema": "question/1", "type": "choice", "instructions": "Which team?",
        "options": [{"value": v, "description": None} for v in ("billing", "tech", True)],
    }  # fmt: skip
    rule = {"id": "select", "action": "SELECT", "scalar": "top", "op": "ge", "threshold": 0.6}
    bundle = _bundle(
        definition,
        calibration={"method": "temperature", "temperature": 1.0},
        rules=[rule | {"per_option": [{"value": "tech", "threshold": 0.9}]}],
        option_actions=["billing"],
        audit_fraction=0,
    )
    client = _loaded(bundle)

    def gate(probs: list[float]) -> GateResult:
        return client.gate("q", probabilities=probs, model_served=MODEL)

    assert (gate([0.1, 0.2, 0.7]).action, gate([0.1, 0.2, 0.7]).label) == ("SELECT:true", True)
    assert gate([0.7, 0.2, 0.1]).action == "BLOCK"  # billing is blocked
    assert gate([0.1, 0.8, 0.1]).action == "ESCALATE"  # tech needs 0.9
    assert gate([0.05, 0.95, 0.0]).action == "SELECT:tech"
    assert gate([0.4, 0.4, 0.2]).label is None  # a tie goes to the first: billing, < 0.6
    assert gate([0.5, 0.5]).reason == "invalid_probabilities"
    assert gate([0.5, 0.6, 0.1]).reason == "invalid_probabilities"  # sums to 1.2


def test_score_rules_read_tail_probabilities() -> None:
    definition = {
        "schema": "question/1", "type": "score", "instructions": "How hard?",
        "levels": [{"label": str(i), "description": None} for i in range(4)],
    }  # fmt: skip
    rules = [
        {"id": "accept", "action": "ACCEPT", "scalar": {"tail_ge": 2}, "op": "lt",
         "threshold": 0.1},
        {"id": "block", "action": "BLOCK", "scalar": {"tail_ge": 3}, "op": "ge",
         "threshold": 0.5},
    ]  # fmt: skip
    client = _loaded(_bundle(definition, calibration={"method": "identity"}, rules=rules,
                             audit_fraction=0))  # fmt: skip

    def gate(probs: list[float]) -> GateResult:
        return client.gate("q", probabilities=probs, model_served=MODEL)

    assert gate([0.6, 0.35, 0.04, 0.01]).action == "ACCEPT"
    assert gate([0.1, 0.1, 0.2, 0.6]).action == "BLOCK"
    middle = gate([0.3, 0.3, 0.3, 0.1])
    assert middle.action == "ESCALATE" and middle.scalar == pytest.approx(0.4)


@pytest.mark.req("FR-34")
def test_a_tampered_policy_is_dropped_and_the_last_good_one_kept() -> None:
    tampered = copy.deepcopy(BUNDLE)
    tampered["policies"][0]["policy"]["rules"][0]["threshold"] = 0.9  # accept almost all
    fresh = build_snapshot(tampered, Snapshot(), time.time())
    assert (QID, MODEL) in fresh.broken and not fresh.policies
    client = _loaded()
    client._snapshot = fresh
    d = client.gate("refund_fraud", p=0.5, model_served=MODEL)
    assert (d.source, d.reason) == ("default", "fingerprint_mismatch")
    kept = build_snapshot(tampered, build_snapshot(BUNDLE, Snapshot(), 0), time.time())
    assert kept.policies[(QID, MODEL)] == BUNDLE["policies"][0]
    with pytest.raises(ValueError):
        build_snapshot({"schema": "bundle/2"}, Snapshot(), 0)


@pytest.mark.req("FR-32")
def test_polling_with_etag_and_a_disk_cache(server: FakeServer, tmp_path: Path) -> None:
    cache = tmp_path / "bundle.json"
    client = _client(server.url, cache_path=cache)
    assert client.wait_for_policies(5)
    assert (
        client.gate("refund_fraud", p=0.83, model_served=MODEL, decision_id=DID).source == "policy"
    )
    assert server.seen[0]["authorization"] == "Bearer tsi_test"
    assert "if-none-match" not in server.seen[0]
    client.refresh()  # unchanged: a 304, and the policies count as confirmed again
    assert server.seen[-1]["if-none-match"] == f'"{BUNDLE["etag"]}"'
    assert client.policy_age_seconds is not None and client.policy_age_seconds < 5
    client.close()
    assert json.loads(cache.read_text()) == BUNDLE
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".trimset-")]

    offline = _client(cache_path=cache)  # the server is unreachable; the cache is enough
    assert offline.wait_for_policies(0)
    assert (
        offline.gate("refund_fraud", p=0.83, model_served=MODEL, decision_id=DID).source == "policy"
    )
    offline.close(0)  # its action report can't be sent; don't wait for it
    cache.write_text("{broken")
    broken = _client(cache_path=cache)
    assert not broken.wait_for_policies(0)
    broken.close()


def test_unreachable_server_never_reaches_gate() -> None:
    client = _client()
    assert not client.wait_for_policies(0.2)
    d = client.gate("refund_fraud", p=0.83, model_served=MODEL)
    assert (d.action, d.reason) == ("ESCALATE", "no_policy")
    client.close()
    with pytest.raises(ValueError):
        Client("file:///etc/passwd", "tsi_x")
    with pytest.raises(ValueError):
        Client("http://x", "")


# ------------------------------------------------------------ reporting back (T3.5)

REQUEST = {
    "model": "jev-latest",
    "state": {"refund_id": "rf-1", "email": "secret-value@example.com"},
    "questions": {
        "fraud": {"type": "noul", "instructions": "Is this refund request fraudulent?"},
        "team": {"type": "choice", "instructions": "Which team?",
                 "criteria": {"billing": None, "tech": None}},
    },
}  # fmt: skip
RESPONSE = {
    "model": MODEL,
    "answers": {
        "fraud": {"type": "noul", "noul": 0.83},
        "team": {"type": "choice", "probabilities": {"billing": 0.7, "tech": 0.3}},
    },
}


def _live(server: FakeServer, **options: Any) -> Client:
    client = _client(server.url, flush_interval=0.05, **options)
    assert client.wait_for_policies(5)
    return client


@pytest.mark.req("FR-01")
@pytest.mark.req("FR-36")
def test_log_exchange_sends_records_with_the_gate_and_a_state_hash(server: FakeServer) -> None:
    client = _live(server)
    d = client.gate("refund_fraud", answer=RESPONSE["answers"]["fraud"], model_served=MODEL)
    extra = {"external_ref": "rf-1", "tags": {"channel": "web"}, "latency_ms": 41}
    ids = client.log_exchange(REQUEST, RESPONSE, format="systemone", gates={"fraud": d}, **extra)
    assert client.flush(5)
    ((path, body),) = server.posts  # no action report: the gate made its own id
    assert path == "/v1/decisions:log" and ids[0] == d.decision_id and len(ids) == 2
    fraud, team = body["records"]
    assert [fraud["decision_id"], team["decision_id"]] == ids
    assert (fraud["action_taken"], fraud["policy_version"]) == (d.action, 7)
    assert "action_taken" not in team and team["probabilities"] == [0.7, 0.3]
    assert fraud["state_hash"] and fraud["state_hash"] == team["state_hash"]
    assert fraud["state_hash_alg"] == "hmac-sha256/1" and fraud["external_ref"] == "rf-1"
    assert set(body["questions"]) == {fraud["question_id"], team["question_id"]}
    assert body["client"]["sdk"] == "trimset-python" and body["client"]["dropped_since_last"] == 0
    assert "secret-value" not in json.dumps(body)  # the state never leaves the app

    client.log_exchange(REQUEST, RESPONSE, format="systemone")  # known questions aren't resent
    assert client.flush(5)
    assert "questions" not in server.posts[-1][1]
    assert client.log_exchange({"no": "model"}, RESPONSE, format="systemone") == []
    client.close()


@pytest.mark.req("FR-33")
def test_a_gate_with_a_proxy_decision_id_reports_its_action(server: FakeServer) -> None:
    client = _live(server)
    d = client.gate("refund_fraud", p=0.83, model_served=MODEL, decision_id=DID)
    miss = client.gate("refund_fraud", p=0.83, model_served="jev-9", decision_id=DID.upper())
    assert client.flush(5)
    reports = [r for path, body in server.posts if path == "/v1/decisions:actions"
               for r in body["actions"]]  # fmt: skip
    assert reports == [
        {"decision_id": DID, "reason": None, "action_taken": d.action, "action_source": "policy",
         "policy_version": 7, "policy_fingerprint": d.policy_fingerprint, "audit": False},
        {"decision_id": DID.upper(), "reason": "model_mismatch", "action_taken": miss.action,
         "action_source": "default", "policy_version": None, "policy_fingerprint": None,
         "audit": False},
    ]  # fmt: skip
    client.close()


@pytest.mark.req("FR-10")
def test_outcomes_check_only_their_shape(
    server: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    client = _live(server)
    client.outcome(decision_id=DID, label=True, source="downstream")
    client.outcome(external_ref="rf-1", question="refund_fraud", label=False, source="rule",
                   observed_at="2026-11-11T10:00:00Z", outcome_id=DID)  # fmt: skip
    with caplog.at_level(logging.WARNING, logger="trimset"):
        client.outcome(label=True, source="downstream")  # neither
        client.outcome(decision_id=DID, external_ref="rf-1", question="q", label=1, source="rule")
        client.outcome(external_ref="rf-1", label=True, source="downstream")  # no question
        client.outcome(decision_id=DID, label=True, source="a hunch")
    assert len(caplog.records) == 4
    assert client.flush(5)
    sent = [o for path, body in server.posts if path == "/v1/outcomes" for o in body["outcomes"]]
    assert [o.get("decision_id") or o["external_ref"] for o in sent] == [DID, "rf-1"]
    assert sent[0]["schema"] == "outcome/1" and sent[0]["observed_at"].endswith("+00:00")
    assert sent[1] == {"schema": "outcome/1", "label": False, "source": "rule",
                       "observed_at": "2026-11-11T10:00:00Z", "external_ref": "rf-1",
                       "question": "refund_fraud", "outcome_id": DID}  # fmt: skip
    client.close()


def test_server_errors_retry_and_client_errors_drop(
    server: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    client = _live(server)
    server.statuses = [503, 429, 200]
    client.outcome(decision_id=DID, label=True, source="downstream")
    assert client.flush(10)  # backoff with jitter, then through, once
    assert [path for path, _ in server.posts] == ["/v1/outcomes"]

    server.statuses = [400]
    with caplog.at_level(logging.WARNING, logger="trimset"):
        for _ in range(2):
            client.outcome(decision_id=DID, label=True, source="downstream")
            assert client.flush(5)
    assert client.dropped == 1 and len(server.posts) == 2
    assert [r.getMessage() for r in caplog.records].count(
        "trimset: dropped 1 outcomes: HTTP 400"
    ) == 1
    client.close()


def test_a_full_queue_drops_the_oldest_and_says_so(server: FakeServer) -> None:
    client = _client(server.url, buffer_size=3, batch_size=100, flush_interval=60)
    for i in range(5):
        client.log({"schema": "record/1", "decision_id": f"d{i}", "question_id": "q"})
    assert client.dropped == 2
    assert client.flush(5)
    ((_, body),) = server.posts
    assert [r["decision_id"] for r in body["records"]] == ["d2", "d3", "d4"]
    assert body["client"]["dropped_since_last"] == 2
    client.log({"schema": "record/1", "decision_id": "d5", "question_id": "q"})
    assert client.flush(5)
    assert server.posts[-1][1]["client"]["dropped_since_last"] == 0
    client.close()


@pytest.mark.req("FR-36")
def test_instrument_logs_an_in_process_model_and_never_gets_in_its_way(
    server: FakeServer,
) -> None:
    client = _live(server)
    model = f"autotrust/GEV-26B-Decide@{'a' * 40}"
    bare = {"fraud": {"type": "noul", "noul": 0.2}}

    def pipeline(request: dict[str, Any]) -> dict[str, Any]:
        return bare

    decide = client.instrument(pipeline, model=model)
    assert decide.__name__ == "pipeline"
    request = {"state": {"x": 1}, "questions": {"fraud": REQUEST["questions"]["fraud"]}}
    assert decide(request) is bare and "model" not in request
    assert decide("not a request") is bare  # can't be logged; the caller still gets its answer
    assert client.flush(5)
    ((_, body),) = server.posts
    (record,) = body["records"]
    assert record["model_served"] == record["model_requested"] == model
    assert record["latency_ms"] >= 0

    def broken(request: Any) -> Any:
        raise RuntimeError("the model failed")

    with pytest.raises(RuntimeError, match="the model failed"):  # the model's own error
        client.instrument(broken, model=model)(request)
    client.close()

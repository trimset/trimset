"""NFR-04: gate() p99 under 1 ms with 1,000 policies cached, and no network call on that
path. NFR-05's SDK half: the default poll interval leaves room for the 60 s bound."""

import copy
import gc
import inspect
import json
import random
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from trimset import Client
from trimset.client import Snapshot, build_snapshot
from trimset.hashing import fingerprint, question_id

EXAMPLE = json.loads(
    (Path(__file__).parents[3] / "docs" / "03-api" / "examples" / "bundle.json").read_text()
)
MODEL = "jev-1.14.0"
POLICIES = 1000
CALLS = 30_000
BUDGET_NS = 1_000_000  # 1 ms


def _bundle() -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """1,000 questions with a policy each, a mix of noul and choice, fingerprinted. Also
    each question's name with the input that is valid for its type."""
    template = EXAMPLE["policies"][0]["policy"]
    questions: dict[str, Any] = {}
    policies = []
    calls = []
    for i in range(POLICIES):
        if i % 4:
            definition = {
                "schema": "question/1", "type": "noul", "instructions": f"Is {i} fraud?",
                "noul": {"true": None, "false": None},
            }  # fmt: skip
            extra: dict[str, Any] = {}
            answer: dict[str, Any] = {"p": 0.4}
        else:
            definition = {
                "schema": "question/1", "type": "choice", "instructions": f"Which team for {i}?",
                "options": [{"value": v, "description": None} for v in ("a", "b", "c", "d")],
            }  # fmt: skip
            extra = {
                "calibration": {"method": "temperature", "temperature": 1.3},
                "rules": [{"id": "select", "action": "SELECT", "scalar": "top", "op": "ge",
                           "threshold": 0.5}],
            }  # fmt: skip
            answer = {"probabilities": [0.1, 0.2, 0.6, 0.1]}
        qid, name = question_id(definition), f"q{i}"
        questions[name] = {"question_id": qid, "definition": definition}
        body = copy.deepcopy(template) | {
            "question_id": qid, "question_name": name, "type": definition["type"],
            "policy_id": f"019a5c00-2222-7aaa-8bbb-{i:012d}", "audit_fraction": 0.01,
        } | extra  # fmt: skip
        body["fingerprint"] = fingerprint(body)
        policies.append({"policy": body, "guarantee_status": "certified"})
        calls.append((name, answer))
    return EXAMPLE | {"questions": questions, "policies": policies}, calls


@pytest.mark.req("NFR-04")
def test_gate_p99_is_under_a_millisecond_with_1000_policies_and_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, calls = _bundle()
    client = Client("http://127.0.0.1:1", "tsi_test", poll_interval=3600)
    client.close(0)
    start = time.perf_counter()
    client._snapshot = build_snapshot(bundle, Snapshot(), time.time())  # verifies 1,000 policies
    assert len(client.snapshot.policies) == POLICIES
    verify_s = time.perf_counter() - start

    touched: list[Any] = []
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: touched.append(a))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: touched.append(a))

    rng = random.Random(1)  # noqa: S311 - a reproducible order, not a secret
    order = [calls[rng.randrange(POLICIES)] for _ in range(CALLS)]
    for name, answer in order[:2000]:  # warm up
        client.gate(name, model_served=MODEL, **answer)
    gc.collect()
    samples = []
    for name, answer in order:
        t = time.perf_counter_ns()
        d = client.gate(name, model_served=MODEL, **answer)
        samples.append(time.perf_counter_ns() - t)
        assert d.source in ("policy", "audit")
    samples.sort()
    p50, p99 = samples[len(samples) // 2], samples[int(len(samples) * 0.99)]
    print(
        f"\ngate(): p50 {p50 / 1000:.1f} us, p99 {p99 / 1000:.1f} us, "
        f"max {samples[-1] / 1000:.0f} us over {CALLS} calls; "
        f"verifying {POLICIES} policies took {verify_s * 1000:.0f} ms"
    )
    assert p99 < BUDGET_NS, f"p99 {p99 / 1000:.0f} us"
    assert touched == [], "gate() touched the network"


@pytest.mark.req("NFR-05")
def test_the_default_poll_interval_leaves_room_for_the_60_second_bound() -> None:
    """An activation or rollback reaches every SDK within 60 s: at most one poll interval
    (with its jitter) after the server has it, plus the poll itself."""
    params = inspect.signature(Client).parameters
    interval, jitter, timeout = (
        params[n].default for n in ("poll_interval", "poll_jitter", "timeout")
    )
    worst = interval * (1 + jitter) + timeout
    assert worst < 60.0, f"{worst} s"

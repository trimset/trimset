import time
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from starlette.testclient import TestClient

from trimset.wire import SUM_TOLERANCE, parse_exchange
from trimset_tools import mock_upstream
from trimset_tools.mock_upstream import Config, create_app, probabilities

SYSTEMONE = {
    "model": "jev-latest",
    "state": {"subject": "Invoice", "message": "Can I get an invoice for last month?"},
    "questions": {
        "intent": {
            "type": "choice",
            "instructions": "What does the customer want?",
            "criteria": {"invoice": "An invoice", "refund": "Money back", "other": None},
        },
        "billing": {"type": "noul", "instructions": "Is this message about billing?"},
        "urgency": {
            "type": "score",
            "instructions": "How urgent is it?",
            "criteria": ["Can wait", "This week", "Today"],
        },
    },
}
OPENAI = {
    "model": "gpt-6-luna",
    "input": "The parcel arrived crushed and the screen is cracked.",
    "questions": [
        {"type": "predicate", "name": "damaged", "instructions": "Is the item damaged?"},
        {
            "type": "choice",
            "name": "ok",
            "instructions": "Approve a refund?",
            "choices": [{"value": True}, {"value": "true", "description": "the word"}],
        },
        {
            "type": "score",
            "name": "severity",
            "instructions": "How bad is it?",
            "levels": [{"label": "minor"}, {"label": "major"}, {"label": "total"}],
        },
    ],
}


def _ask(c: TestClient, body: Any = SYSTEMONE, path: str = "/v1/systemone") -> Any:
    return c.post(path, json=body)


@pytest.mark.req("FR-59")
@pytest.mark.parametrize(
    ("path", "request_", "fmt"),
    [("/v1/systemone", SYSTEMONE, "systemone"), ("/v1/decisions", OPENAI, "openai-decisions")],
)
def test_answers_follow_the_wire_format(path: str, request_: Any, fmt: Any) -> None:
    r = _ask(TestClient(create_app()), request_, path)
    assert r.status_code == 200
    ex = parse_exchange(request_, r.json(), fmt)
    assert ex.skipped == []
    assert len(ex.decisions) == 3
    assert [len(d.probabilities or []) for d in ex.decisions] == (
        [3, 1, 3] if fmt == "systemone" else [1, 2, 3]
    )
    assert ex.usage.input_tokens > 0


@pytest.mark.req("FR-59")
def test_the_same_seed_gives_the_same_answers_and_another_seed_others() -> None:
    first = _ask(TestClient(create_app(Config(seed=7)))).json()
    again = _ask(TestClient(create_app(Config(seed=7)))).json()
    other = _ask(TestClient(create_app(Config(seed=8)))).json()
    assert first == again
    assert first["answers"] != other["answers"]


@pytest.mark.req("FR-59")
def test_a_new_version_is_a_silent_upgrade_that_moves_the_answers() -> None:
    c = TestClient(create_app())
    before = _ask(c).json()
    assert before["model"] == "jev-1.0.0"
    assert c.patch("/_mock/config", json={"version": "1.1.0"}).json()["version"] == "1.1.0"
    after = _ask(c).json()
    assert after["model"] == "jev-1.1.0"
    assert after["answers"] != before["answers"]
    pinned = _ask(c, SYSTEMONE | {"model": "jev-1.0.0"}).json()
    assert pinned["model"] == "jev-1.0.0"  # a pinned name is echoed
    assert _ask(c, OPENAI, "/v1/decisions").json()["model"] == "gpt-6-luna"  # as OpenAI does


@given(
    seed=st.integers(0, 2**32),
    version=st.text(min_size=1, max_size=10),
    state=st.text(max_size=20),
    k=st.integers(1, 255),
)
def test_probabilities_are_valid_for_any_size(seed: int, version: str, state: str, k: int) -> None:
    p = probabilities(Config(seed=seed, version=version), state, "q", k)
    assert len(p) == k
    assert all(0 <= x <= 1 for x in p)
    if k > 1:
        assert abs(sum(p) - 1) <= SUM_TOLERANCE


def test_a_version_shifts_the_distribution_not_just_single_answers() -> None:
    def mean_yes(version: str) -> float:
        config = Config(version=version)
        return sum(probabilities(config, i, "q", 1)[0] for i in range(2000)) / 2000

    # Different versions lean differently, so drift checks on the scores can see them.
    assert abs(mean_yes("1.0.0") - mean_yes("2.0.0")) > 0.02


@pytest.mark.req("FR-59")
def test_errors_are_injected_in_the_formats_own_shape() -> None:
    c = TestClient(create_app(Config(error_rate=1.0, error_status=429)))
    r = _ask(c)
    assert (r.status_code, r.headers["retry-after"], r.json()) == (
        429,
        "1",
        {"detail": "the mock upstream failed on purpose"},
    )
    c.patch("/_mock/config", json={"error_status": 503})
    r = _ask(c, OPENAI, "/v1/decisions")
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "server_error"


def test_a_partial_error_rate_is_the_same_sequence_every_run() -> None:
    def statuses() -> list[int]:
        c = TestClient(create_app(Config(seed=3, error_rate=0.5)))
        return [_ask(c).status_code for _ in range(40)]

    first = statuses()
    assert first == statuses()
    assert 5 < first.count(503) < 35


@pytest.mark.req("FR-59")
def test_latency_is_injected() -> None:
    c = TestClient(create_app(Config(latency_ms=60)))
    started = time.monotonic()
    assert _ask(c).status_code == 200
    assert time.monotonic() - started >= 0.06


def test_the_last_request_is_shown() -> None:
    c = TestClient(create_app())
    assert c.get("/_mock/last").json() == {"count": 0, "last": None}
    c.post("/v1/systemone", json=SYSTEMONE, headers={"Authorization": "Bearer app-key"})
    seen = c.get("/_mock/last").json()
    assert seen["count"] == 1
    assert seen["last"]["path"] == "/v1/systemone"
    assert seen["last"]["headers"]["authorization"] == "Bearer app-key"


@pytest.mark.parametrize(
    "patch",
    [
        {"surprise": 1},
        {"error_rate": 2},
        {"latency_ms": -1},
        {"seed": "7"},
        {"version": ""},
        {"error_status": 200},
        [1],
    ],
)
def test_bad_config_changes_are_refused(patch: Any) -> None:
    c = TestClient(create_app())
    assert c.patch("/_mock/config", json=patch).status_code == 400
    assert c.get("/_mock/config").json() == {
        "seed": 0,
        "version": "1.0.0",
        "latency_ms": 0.0,
        "error_rate": 0.0,
        "error_status": 503,
    }


@pytest.mark.parametrize(
    ("path", "body", "message"),
    [
        ("/v1/systemone", {"questions": {}}, "the body must be an object with a `model`"),
        ("/v1/systemone", {"model": "m", "questions": {}}, "`questions` must be a non-empty"),
        ("/v1/systemone", {"model": "m", "questions": {"q": {"type": "x"}}}, "question 'q'"),
        ("/v1/decisions", {"model": "m", "questions": []}, "`questions` must be an array"),
        ("/v1/decisions", {"model": "m", "questions": [{"type": "choice"}]}, "questions[0]"),
        (
            "/v1/decisions",
            {"model": "m", "questions": [{"type": "score", "levels": [1]}]},
            "questions[0]: each",
        ),
    ],
)
def test_bad_requests_get_400(path: str, body: Any, message: str) -> None:
    r = TestClient(create_app()).post(path, json=body)
    assert r.status_code == 400
    text = r.json()["detail"] if path == "/v1/systemone" else r.json()["error"]["message"]
    assert text.startswith(message)


def test_only_json_bodies() -> None:
    c = TestClient(create_app())
    r = c.post("/v1/systemone", content=b"x", headers={"content-type": "text/plain"})
    assert r.status_code == 415
    r = c.post("/v1/systemone", content=b"{", headers={"content-type": "application/json"})
    assert (r.status_code, r.json()) == (400, {"detail": "the body is not JSON"})
    r = c.post("/v1/systemone", content=b'{"x": NaN}', headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_models_and_health() -> None:
    c = TestClient(create_app())
    assert c.get("/v1/models").json()["data"][0]["id"] == "mock-latest"
    assert c.get("/health").json() == {"status": "ok"}


def test_the_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: dict[str, Any] = {}
    monkeypatch.setattr(mock_upstream.uvicorn, "run", lambda app, **kw: ran.update(app=app, **kw))
    mock_upstream.main(["--port", "9100", "--seed", "4", "--version", "2.0.0"])
    assert (ran["host"], ran["port"]) == ("127.0.0.1", 9100)
    assert ran["app"].state.config == Config(seed=4, version="2.0.0")
    with pytest.raises(SystemExit):
        mock_upstream.main(["--error-rate", "3"])

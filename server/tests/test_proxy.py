"""The proxy against a fake upstream, with no database: forwarding must not need one."""

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from trimset.hashing import project_state_key, state_hash
from trimset_server.app import create_app
from trimset_server.auth import KeyCache
from trimset_server.config import Settings
from trimset_server.keys import Principal, hash_key, new_key
from trimset_server.projects import ProjectCache, ProjectSettings
from trimset_server.upstreams import Upstream

MISSING = "A trimset key is required: /p/<key> in the base URL, or X-Trimset-Key"
SECRET = b"proxy-test-secret-0123456789abcdef"
PROJECT = uuid.uuid4()
REQUEST = {
    "model": "jev-latest",
    "state": {"refund_id": "rf-1001"},
    "questions": {
        "fraud": {"type": "noul", "instructions": "Is this refund request fraudulent?"},
        "team": {
            "type": "choice",
            "instructions": "Which team?",
            "criteria": {"a": None, "b": None},
        },
    },
}
ANSWER = {
    "model": "jev-1.14.0",
    "answers": {
        "fraud": {"type": "noul", "noul": 0.83},
        "team": {
            "type": "choice",
            "choice": "a",
            "confidence": 0.6,
            "probabilities": {"a": 0.8, "b": 0.2},
        },
    },
    "usage": {"input_tokens": 1000, "output_tokens": 0},
}


class Upstreams:
    """A stand-in for the vendor: records each request, answers with `reply`."""

    def __init__(self) -> None:
        self.seen: list[httpx2.Request] = []
        self.reply: Callable[[httpx2.Request], httpx2.Response] = lambda r: httpx2.Response(
            200, json=ANSWER, headers={"x-request-id": "up-1", "connection": "close"}
        )

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        return self.reply(request)


@pytest.fixture
def proxy() -> Iterator[tuple[TestClient, Upstreams, dict[str, str], dict[str, ProjectSettings]]]:
    fake = Upstreams()
    keys = {scope: new_key(scope) for scope in ("proxy", "ingest", "review")}  # type: ignore[arg-type]
    principals = {
        hash_key(k): Principal(key_id=uuid.uuid4(), project_id=PROJECT, scope=s)  # type: ignore[arg-type]
        for s, k in keys.items()
    }
    project_settings = {
        "settings": ProjectSettings.model_validate({"prices": {"jev-1.14.0": {"input": 0.042}}})
    }
    settings = Settings(
        database_url="postgresql://u:p@127.0.0.1:1/trimset",  # unreachable on purpose
        secret=SECRET,
        upstreams={"jev": Upstream("jev", "https://upstream.test", "systemone", key="sk-operator")},
    )

    async def load_key(digest: bytes) -> Principal | None:
        return principals.get(digest)

    async def load_project(project_id: uuid.UUID) -> ProjectSettings | None:
        return project_settings["settings"] if project_id == PROJECT else None

    with TestClient(create_app(settings, transport=httpx2.MockTransport(fake))) as c:
        c.app.state.keys = KeyCache(load_key, ttl_s=30)  # type: ignore[attr-defined]
        c.app.state.projects = ProjectCache(load_project, ttl_s=0)  # type: ignore[attr-defined]
        yield c, fake, keys, project_settings


def _pending(c: TestClient) -> list[Any]:
    return list(c.app.state.recorder._queue)  # type: ignore[attr-defined]


@pytest.mark.req("FR-04")
@pytest.mark.req("NFR-06")
def test_path_mode_forwards_unchanged_and_queues_records_without_a_database(proxy: Any) -> None:
    c, fake, keys, _ = proxy
    r = c.post(
        f"/p/{keys['proxy']}/v1/systemone",
        json=REQUEST,
        headers={
            "Authorization": "Bearer sk-app-own-key",
            "X-Trimset-External-Ref": "rf-1001",
            "X-Trimset-Tags": "channel=web,country=DE",
        },
    )
    assert r.status_code == 200
    assert r.json() == ANSWER  # the upstream's body, unchanged
    assert r.headers["x-request-id"] == "up-1"
    ids = r.headers["x-trimset-decision-id"].split(",")
    assert len(ids) == 2 and all(ids)

    (sent,) = fake.seen
    assert str(sent.url) == "https://upstream.test/v1/systemone"
    assert sent.headers["authorization"] == "Bearer sk-app-own-key"  # passthrough
    assert not any(k.lower().startswith("x-trimset") for k in sent.headers)
    assert json.loads(sent.content) == REQUEST

    pending = _pending(c)
    assert [str(p.record.decision_id) for p in pending] == ids
    fraud = pending[0].record
    assert (fraud.question_name, fraud.model_requested, fraud.model_served) == (
        "fraud",
        "jev-latest",
        "jev-1.14.0",
    )
    assert fraud.probabilities == [0.83]
    assert fraud.external_ref == "rf-1001"
    assert fraud.tags == {"channel": "web", "country": "DE"}
    key = project_state_key(SECRET, str(PROJECT))
    assert fraud.state_hash == state_hash(key, REQUEST["state"])
    assert fraud.cost_usd == pytest.approx(1000 * 0.042 / 1e6 / 2)  # split across 2 questions
    assert fraud.latency_ms is not None and fraud.latency_ms >= 0


@pytest.mark.req("FR-08")
def test_header_mode_and_ingest_keys_work_too(proxy: Any) -> None:
    c, _, keys, _ = proxy
    r = c.post("/v1/systemone", json=REQUEST, headers={"X-Trimset-Key": keys["ingest"]})
    assert r.status_code == 200 and "x-trimset-decision-id" in r.headers


@pytest.mark.req("FR-06")
def test_server_credentials_replace_the_apps_and_trimset_keys_never_leave(proxy: Any) -> None:
    c, fake, keys, project = proxy
    project["settings"] = ProjectSettings.model_validate(
        {"upstream": {"name": "jev", "credentials": "server"}}
    )
    c.post(
        f"/p/{keys['proxy']}/v1/systemone", json=REQUEST, headers={"Authorization": "Bearer sk-app"}
    )
    assert fake.seen[-1].headers["authorization"] == "Bearer sk-operator"

    project["settings"] = ProjectSettings.model_validate({})
    c.post(
        "/v1/systemone",
        json=REQUEST,
        headers={"X-Trimset-Key": keys["proxy"], "Authorization": f"Bearer {keys['proxy']}"},
    )
    assert "authorization" not in fake.seen[-1].headers  # an app's mistake, not forwarded


@pytest.mark.parametrize(
    ("path", "headers", "status", "body"),
    [
        (
            "/v1/systemone",
            {},
            401,
            {"detail": MISSING},
        ),
        (
            "/p/tsp_" + "x" * 32 + "/v1/systemone",
            {},
            401,
            {"detail": "Unknown or revoked trimset key"},
        ),
        (
            "/v1/decisions",
            {},
            401,
            {
                "error": {
                    "message": MISSING,
                    "type": "trimset_error",
                    "code": "missing_key",
                    "param": None,
                }
            },
        ),
    ],
)
def test_trimset_errors_use_the_upstreams_format(
    proxy: Any, path: str, headers: dict[str, str], status: int, body: Any
) -> None:
    c, fake, _, _ = proxy
    r = c.post(path, json=REQUEST, headers=headers)
    assert (r.status_code, r.json()) == (status, body)
    assert fake.seen == []


@pytest.mark.req("FR-52")
def test_a_review_key_cant_use_the_proxy(proxy: Any) -> None:
    c, fake, keys, _ = proxy
    r = c.post(f"/p/{keys['review']}/v1/systemone", json=REQUEST)
    assert r.status_code == 403 and fake.seen == []


@pytest.mark.parametrize(
    ("reply", "status"),
    [
        (lambda r: (_ for _ in ()).throw(httpx2.ConnectError("down", request=r)), 502),
        (lambda r: (_ for _ in ()).throw(httpx2.ReadTimeout("slow", request=r)), 504),
    ],
)
def test_an_unreachable_or_slow_upstream_gets_502_or_504(
    proxy: Any, reply: Any, status: int
) -> None:
    c, fake, keys, _ = proxy
    fake.reply = reply
    r = c.post(f"/p/{keys['proxy']}/v1/systemone", json=REQUEST)
    assert r.status_code == status
    assert "detail" in r.json()
    assert _pending(c) == []


def test_upstream_errors_and_redirects_pass_through_unlogged(proxy: Any) -> None:
    c, fake, keys, _ = proxy
    fake.reply = lambda r: httpx2.Response(422, json={"detail": [{"msg": "Field required"}]})
    r = c.post(f"/p/{keys['proxy']}/v1/systemone", json=REQUEST)
    assert (r.status_code, r.json()) == (422, {"detail": [{"msg": "Field required"}]})
    assert "x-trimset-decision-id" not in r.headers

    fake.reply = lambda r: httpx2.Response(307, headers={"location": "http://169.254.169.254/"})
    r = c.post(f"/p/{keys['proxy']}/v1/systemone", json=REQUEST, follow_redirects=False)
    assert r.status_code == 307 and len(fake.seen) == 2  # handed back, never followed
    assert _pending(c) == []


def test_too_large_bodies_are_refused_before_forwarding(proxy: Any) -> None:
    c, fake, keys, project = proxy
    project["settings"] = ProjectSettings.model_validate({"proxy_max_body_mb": 1})
    r = c.post(f"/p/{keys['proxy']}/v1/systemone", content=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413 and fake.seen == []


def test_an_unreadable_exchange_is_forwarded_but_not_logged(proxy: Any) -> None:
    c, fake, keys, _ = proxy
    fake.reply = lambda r: httpx2.Response(200, content=b"not json")
    r = c.post(f"/p/{keys['proxy']}/v1/systemone", json=REQUEST)
    assert (r.status_code, r.content) == (200, b"not json")
    assert "x-trimset-decision-id" not in r.headers


def test_a_skipped_question_keeps_its_slot_in_the_header(proxy: Any) -> None:
    c, fake, keys, _ = proxy
    answer = json.loads(json.dumps(ANSWER))
    del answer["answers"]["fraud"]
    fake.reply = lambda r: httpx2.Response(200, json=answer)
    r = c.post(f"/p/{keys['proxy']}/v1/systemone", json=REQUEST)
    first, second = r.headers["x-trimset-decision-id"].split(",")
    assert first == "" and second


def test_openai_format_and_models_pass_through(proxy: Any) -> None:
    c, fake, keys, project = proxy
    project["settings"] = ProjectSettings.model_validate({})
    c.app.state.settings = Settings(  # type: ignore[attr-defined]
        database_url="postgresql://u:p@127.0.0.1:1/trimset",
        upstreams={"oa": Upstream("oa", "https://oa.test", "openai-decisions")},
    )
    fake.reply = lambda r: httpx2.Response(
        200, json={"model": "gpt-6-luna", "answers": [{"type": "refusal", "name": "q"}]}
    )
    body = {
        "model": "gpt-6-luna",
        "input": "hi",
        "questions": [{"type": "predicate", "name": "q", "instructions": "Is it?"}],
    }
    r = c.post(f"/p/{keys['proxy']}/v1/decisions", json=body)
    assert r.status_code == 200
    assert _pending(c)[-1].record.refusal is True

    fake.reply = lambda r: httpx2.Response(200, json={"models": [{"name": "gpt-6-luna"}]})
    r = c.get(f"/p/{keys['proxy']}/v1/models")
    assert r.json() == {"models": [{"name": "gpt-6-luna"}]}
    assert fake.seen[-1].method == "GET"


def test_multipart_requests_log_their_request_part_and_forward_the_image(proxy: Any) -> None:
    c, fake, keys, _ = proxy
    r = c.post(
        f"/p/{keys['proxy']}/v1/systemone",
        files={
            "request": (None, json.dumps(REQUEST), "application/json"),
            "img1": ("a.png", b"\x89PNG-bytes", "image/png"),
        },
    )
    assert r.status_code == 200 and "x-trimset-decision-id" in r.headers
    assert b"\x89PNG-bytes" in fake.seen[-1].content


@pytest.mark.req("NFR-06")
def test_a_full_buffer_drops_the_oldest_and_counts(proxy: Any) -> None:
    c, _, keys, _ = proxy
    recorder = c.app.state.recorder  # type: ignore[attr-defined]
    recorder._capacity = 3
    for _ in range(3):
        c.post(f"/p/{keys['proxy']}/v1/systemone", json=REQUEST)  # 2 records each
    assert recorder.pending == 3
    assert recorder.dropped == 3


@pytest.mark.req("FR-09")
def test_the_tag_allow_list_holds_on_the_proxy_too(proxy: Any) -> None:
    c, _, keys, project = proxy
    project["settings"] = ProjectSettings.model_validate(
        {"privacy": {"tag_allowlist": ["channel"]}}
    )
    r = c.post(
        f"/p/{keys['proxy']}/v1/systemone",
        json=REQUEST,
        headers={"X-Trimset-Tags": "channel=web,customer_email=ann%40example.com"},
    )
    assert r.status_code == 200  # the app's call never suffers for its tags
    assert {p.record.tags["channel"] for p in _pending(c)} == {"web"}
    assert all("customer_email" not in p.record.tags for p in _pending(c))

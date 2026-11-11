"""The request envelope of POST /v1/decisions:log. These fail before any database work."""

import json
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from trimset_server.app import create_app
from trimset_server.auth import KeyCache
from trimset_server.config import Settings
from trimset_server.keys import Principal, hash_key, new_key

UNREACHABLE = Settings(database_url="postgresql://u:p@127.0.0.1:1/trimset")


@pytest.fixture
def client() -> Iterator[tuple[TestClient, dict[str, str]]]:
    keys = {scope: new_key(scope) for scope in ("proxy", "ingest", "admin")}  # type: ignore[arg-type]
    table = {
        hash_key(k): Principal(key_id=uuid.uuid4(), project_id=uuid.uuid4(), scope=s)  # type: ignore[arg-type]
        for s, k in keys.items()
    }

    async def load(digest: bytes) -> Principal | None:
        return table.get(digest)

    with TestClient(create_app(UNREACHABLE)) as c:
        c.app.state.keys = KeyCache(load, ttl_s=30)  # type: ignore[attr-defined]
        yield c, {s: f"Bearer {k}" for s, k in keys.items()}


def _post(c: TestClient, auth: str, content: bytes) -> object:
    return c.post(
        "/v1/decisions:log",
        content=content,
        headers={"Authorization": auth, "Content-Type": "application/json"},
    )


def _problem(r: object, status: int, slug: str) -> None:
    assert r.status_code == status, r.text  # type: ignore[attr-defined]
    assert r.json()["type"].endswith("/" + slug)  # type: ignore[attr-defined]


@pytest.mark.req("FR-52")
def test_only_ingest_and_admin_keys_may_log(client: tuple[TestClient, dict[str, str]]) -> None:
    c, auth = client
    _problem(_post(c, auth["proxy"], b"{}"), 403, "forbidden")
    _problem(c.post("/v1/decisions:log", json={}), 401, "unauthorized")


@pytest.mark.req("FR-01")
@pytest.mark.parametrize(
    ("body", "status", "slug"),
    [
        (b"not json", 400, "invalid-request"),
        (b'{"records": [{"probabilities": [NaN]}]}', 400, "invalid-request"),
        (b'{"records": [Infinity]}', 400, "invalid-request"),
        (b"[]", 400, "invalid-request"),
        (b'{"records": []}', 400, "invalid-request"),
        (b'{"records": {}}', 400, "invalid-request"),
        (b'{"records": [1], "extra": true}', 400, "invalid-request"),
        (b'{"records": [1], "questions": []}', 400, "invalid-request"),
        (json.dumps({"records": [{}] * 1001}).encode(), 413, "too-large"),
        (b'{"records": ["' + b"x" * (1024 * 1024) + b'"]}', 413, "too-large"),
    ],
)
def test_bad_envelopes_are_refused_before_any_work(
    client: tuple[TestClient, dict[str, str]], body: bytes, status: int, slug: str
) -> None:
    c, auth = client
    _problem(_post(c, auth["ingest"], body), status, slug)


def test_nul_anywhere_is_found() -> None:
    from trimset_server.ingest import _has_nul

    assert _has_nul({"a": ["x", {"b\x00": 1}]})
    assert _has_nul(["ok", "no\x00"])
    assert not _has_nul({"a": ["x", {"b": 1, "c": None}]})

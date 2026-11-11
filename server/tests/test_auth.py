import uuid
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from trimset_server import errors
from trimset_server.auth import KeyCache, require
from trimset_server.keys import Principal, hash_key, new_key

ADMIN = Principal(key_id=uuid.uuid4(), project_id=uuid.uuid4(), scope="admin")
INGEST = Principal(key_id=uuid.uuid4(), project_id=uuid.uuid4(), scope="ingest")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Keys:
    """A stand-in for the api_keys table."""

    def __init__(self) -> None:
        self.live: dict[bytes, Principal] = {}
        self.loads = 0

    def add(self, principal: Principal) -> str:
        key = new_key(principal.scope)
        self.live[hash_key(key)] = principal
        return key

    async def load(self, digest: bytes) -> Principal | None:
        self.loads += 1
        return self.live.get(digest)


@pytest.mark.anyio
@pytest.mark.req("FR-52")
async def test_a_revoked_key_is_refused_once_the_ttl_passes() -> None:
    table, clock = Keys(), Clock()
    cache = KeyCache(table.load, ttl_s=30, clock=clock)
    key = table.add(ADMIN)

    assert await cache.principal(key) == ADMIN
    table.live.clear()  # revoked in the database
    clock.now += 29
    assert await cache.principal(key) == ADMIN  # still cached
    assert table.loads == 1
    clock.now += 2
    assert await cache.principal(key) is None  # reloaded and gone
    assert table.loads == 2


@pytest.mark.anyio
async def test_forget_drops_a_key_at_once() -> None:
    table = Keys()
    cache = KeyCache(table.load, ttl_s=30)
    key = table.add(ADMIN)
    await cache.principal(key)
    table.live.clear()
    cache.forget(ADMIN.key_id)
    assert await cache.principal(key) is None


@pytest.mark.anyio
async def test_unknown_and_malformed_keys_are_never_cached() -> None:
    table = Keys()
    cache = KeyCache(table.load, ttl_s=30)
    unknown = new_key("admin")
    assert await cache.principal(unknown) is None
    assert await cache.principal(unknown) is None
    assert table.loads == 2  # asked again each time
    assert await cache.principal("not-a-key") is None
    assert table.loads == 2  # a malformed key never reaches the database


class Body(BaseModel):
    n: int


@pytest.fixture
def client() -> tuple[TestClient, Keys]:
    table = Keys()
    app = FastAPI()
    errors.install(app)
    app.state.keys = KeyCache(table.load, ttl_s=30)

    @app.get("/admin-only")
    async def admin_only(p: Annotated[Principal, Depends(require("admin"))]) -> dict[str, str]:
        return {"project": str(p.project_id)}

    @app.post("/body")
    async def body(b: Body) -> dict[str, int]:
        return {"n": b.n}

    return TestClient(app), table


def _problem(response: object, status: int, slug: str) -> None:
    assert response.status_code == status  # type: ignore[attr-defined]
    assert response.headers["content-type"] == "application/problem+json"  # type: ignore[attr-defined]
    assert response.json()["type"] == f"https://trimset.dev/problems/{slug}"  # type: ignore[attr-defined]


@pytest.mark.req("FR-52")
def test_no_key_is_401_with_a_challenge(client: tuple[TestClient, Keys]) -> None:
    c, _ = client
    r = c.get("/admin-only")
    _problem(r, 401, "unauthorized")
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.req("FR-52")
@pytest.mark.parametrize("header", ["Basic abc", "Bearer ", "Bearer " + new_key("admin"), "tsa_x"])
def test_wrong_scheme_or_unknown_key_is_401(client: tuple[TestClient, Keys], header: str) -> None:
    c, _ = client
    _problem(c.get("/admin-only", headers={"Authorization": header}), 401, "unauthorized")


@pytest.mark.req("FR-52")
def test_a_key_without_the_capability_is_403(client: tuple[TestClient, Keys]) -> None:
    c, table = client
    key = table.add(INGEST)
    _problem(c.get("/admin-only", headers={"Authorization": f"Bearer {key}"}), 403, "forbidden")


@pytest.mark.req("FR-51")
def test_the_right_key_reaches_its_own_project(client: tuple[TestClient, Keys]) -> None:
    c, table = client
    key = table.add(ADMIN)
    r = c.get("/admin-only", headers={"authorization": f"bearer {key}"})
    assert r.status_code == 200
    assert r.json() == {"project": str(ADMIN.project_id)}


def test_invalid_bodies_are_422_problems_without_the_values(
    client: tuple[TestClient, Keys],
) -> None:
    c, _ = client
    r = c.post("/body", json={"n": "secret-looking-value"})
    _problem(r, 422, "invalid-request")
    assert "secret-looking-value" not in r.text
    assert r.json()["errors"][0]["pointer"] == "/body/n"


class FailingKeys(Keys):
    """A key table whose database can go down."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    async def load(self, digest: bytes) -> Principal | None:
        if self.down:
            raise ConnectionRefusedError("database down")
        return await super().load(digest)


@pytest.mark.anyio
@pytest.mark.req("NFR-06")
async def test_only_the_proxy_may_use_a_stale_key_and_only_briefly() -> None:
    from trimset_server.auth import Unavailable

    table, clock = FailingKeys(), Clock()
    cache = KeyCache(table.load, ttl_s=30, clock=clock, max_stale_s=600)
    key = table.add(ADMIN)
    await cache.principal(key)
    table.down = True
    clock.now += 31  # past the TTL, so the database is asked again

    with pytest.raises(Unavailable):
        await cache.principal(key)  # admin, ingest and review routes fail closed
    assert await cache.principal(key, allow_stale=True) == ADMIN  # the proxy keeps going
    clock.now += 600
    with pytest.raises(Unavailable):
        await cache.principal(key, allow_stale=True)  # but not for ever

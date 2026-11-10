from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from trimset_server.app import create_app
from trimset_server.config import Settings

# Port 1 refuses connections at once, so these tests need no database.
UNREACHABLE = Settings(database_url="postgresql://u:p@127.0.0.1:1/trimset")


class FakeConnection:
    async def fetchval(self, query: str) -> int:
        assert query == "SELECT 1"
        return 1


class FakePool:
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeConnection]:
        yield FakeConnection()

    async def close(self) -> None:
        pass


@pytest.mark.req("NFR-13")
def test_health_needs_nothing() -> None:
    with TestClient(create_app(UNREACHABLE)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.req("NFR-13")
def test_ready_is_503_while_postgres_is_unreachable() -> None:
    with TestClient(create_app(UNREACHABLE)) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}  # no exception text leaks out


@pytest.mark.req("NFR-13")
def test_ready_is_200_when_postgres_answers() -> None:
    with TestClient(create_app(UNREACHABLE)) as client:
        client.app.state.pool = FakePool()  # type: ignore[attr-defined]
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_generated_docs_are_off() -> None:
    with TestClient(create_app(UNREACHABLE)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404

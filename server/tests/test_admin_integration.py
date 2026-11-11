"""The admin API and the bootstrap commands, against a real Postgres (CI)."""

import json
from typing import Any

import anyio
import asyncpg
import pytest
from fastapi.testclient import TestClient

from trimset_server import cli, keys
from trimset_server.app import create_app
from trimset_server.config import Settings
from trimset_server.migrate import run_migrations

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _bootstrap(dsn: str, *projects: str) -> dict[str, str]:
    """Each project with one admin key: {project name: key}."""
    await run_migrations(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        out = {}
        for name in projects:
            pid = await keys.create_project(conn, name)
            _, out[name] = await keys.create_key(conn, pid, "admin", "bootstrap")
        return out
    finally:
        await conn.close()


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.req("FR-52")
async def test_keys_are_created_listed_and_revoked(dsn: str) -> None:
    admin = (await _bootstrap(dsn, "acme"))["acme"]
    app = create_app(Settings(database_url=dsn, key_cache_ttl_s=0))
    with TestClient(app) as c:
        r = c.post(
            "/v1/admin/keys", json={"scope": "ingest", "label": "webhook"}, headers=_auth(admin)
        )
        assert r.status_code == 201
        created = r.json()
        assert created["secret"].startswith("tsi_")
        assert created["prefix"] == created["secret"][:8]

        listed = c.get("/v1/admin/keys", headers=_auth(admin)).json()["items"]
        assert [k["label"] for k in listed] == ["bootstrap", "webhook"]
        assert all("secret" not in k and "key_hash" not in k for k in listed)
        assert listed[0]["last_used_at"] is not None  # the admin key has been used

        ingest = created["secret"]
        assert c.get("/v1/admin/keys", headers=_auth(ingest)).status_code == 403

        assert (
            c.delete(f"/v1/admin/keys/{created['key_id']}", headers=_auth(admin)).status_code == 204
        )
        assert c.get("/v1/admin/keys", headers=_auth(ingest)).status_code == 401
        assert (
            c.delete(f"/v1/admin/keys/{created['key_id']}", headers=_auth(admin)).status_code == 204
        )


@pytest.mark.req("FR-51")
async def test_projects_never_see_each_others_keys(dsn: str) -> None:
    admins = await _bootstrap(dsn, "acme", "globex")
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        acme_keys = c.get("/v1/admin/keys", headers=_auth(admins["acme"])).json()["items"]
        globex_keys = c.get("/v1/admin/keys", headers=_auth(admins["globex"])).json()["items"]
        assert {k["key_id"] for k in acme_keys}.isdisjoint(k["key_id"] for k in globex_keys)

        r = c.delete(f"/v1/admin/keys/{acme_keys[0]['key_id']}", headers=_auth(admins["globex"]))
        assert r.status_code == 404
        assert c.get("/v1/admin/keys", headers=_auth(admins["acme"])).status_code == 200


async def test_invalid_key_requests_are_refused(dsn: str) -> None:
    admin = (await _bootstrap(dsn, "acme"))["acme"]
    with TestClient(create_app(Settings(database_url=dsn))) as c:
        for body in (
            {"scope": "root", "label": "x"},
            {"scope": "admin", "label": ""},
            {"scope": "admin"},
        ):
            r = c.post("/v1/admin/keys", json=body, headers=_auth(admin))
            assert r.status_code == 422
            assert r.headers["content-type"] == "application/problem+json"


async def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    """Run a command the way a shell would: on a thread with no event loop of its own,
    because the CLI starts one with asyncio.run()."""
    code = await anyio.to_thread.run_sync(cli.main, list(argv))
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if code == 0 else captured.err)


@pytest.mark.req("FR-53")
async def test_bootstrap_commands_create_a_working_first_key(
    dsn: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    await run_migrations(dsn)
    monkeypatch.setenv("TRIMSET_DATABASE_URL", dsn)
    monkeypatch.delenv("TRIMSET_AUTO_MIGRATE", raising=False)

    code, out = await _run(capsys, "create-project", "acme")
    assert code == 0 and "project_id" in out
    code, err = await _run(capsys, "create-project", "acme")
    assert code == 1 and "already exists" in err

    code, out = await _run(capsys, "create-key", "acme", "--label", "me")
    assert code == 0 and out["scope"] == "admin" and keys.looks_like_key(out["key"])
    code, err = await _run(capsys, "create-key", "nobody")
    assert code == 1 and "no project named" in err

    with TestClient(create_app(Settings(database_url=dsn, key_cache_ttl_s=0))) as c:
        assert c.get("/v1/admin/keys", headers=_auth(out["key"])).status_code == 200
        code, _ = await _run(capsys, "revoke-key", out["key_id"])
        assert code == 0
        assert c.get("/v1/admin/keys", headers=_auth(out["key"])).status_code == 401

    code, err = await _run(capsys, "revoke-key", "00000000-0000-0000-0000-000000000000")
    assert code == 1 and "no key with id" in err


async def test_bootstrap_on_an_unmigrated_database_says_what_to_do(
    dsn: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRIMSET_DATABASE_URL", dsn)
    monkeypatch.delenv("TRIMSET_AUTO_MIGRATE", raising=False)
    code, err = await _run(capsys, "create-project", "acme")
    assert code == 1 and "trimset-server migrate" in err

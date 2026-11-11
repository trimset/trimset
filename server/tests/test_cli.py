import json
from typing import Any

import pytest

from trimset_server import cli
from trimset_server.migrate import MigrationError


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("TRIMSET_DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("TRIMSET_AUTO_MIGRATE", raising=False)
    return monkeypatch


def _record_migrations(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[str]:
    calls: list[str] = []

    async def fake(dsn: str) -> list[str]:
        calls.append(dsn)
        if isinstance(result, Exception):
            raise result
        return list(result)

    monkeypatch.setattr(cli, "run_migrations", fake)
    return calls


def test_serve_runs_uvicorn_with_the_app_factory(env: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    env.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    migrations = _record_migrations(env, [])

    assert cli.main(["serve", "--port", "9000", "--workers", "2"]) == 0

    (call,) = calls
    assert call["app"] == "trimset_server.app:create_app"
    assert call["factory"] is True
    assert (call["host"], call["port"], call["workers"]) == ("0.0.0.0", 9000, 2)
    assert call["server_header"] is False
    assert migrations == []  # auto-migrate is off unless asked for


def test_serve_migrates_first_when_auto_migrate_is_on(env: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    env.setenv("TRIMSET_AUTO_MIGRATE", "true")
    env.setattr(cli.uvicorn, "run", lambda app, **kw: order.append("serve"))

    async def fake(dsn: str) -> list[str]:
        order.append("migrate")
        return []

    env.setattr(cli, "run_migrations", fake)
    assert cli.main(["serve"]) == 0
    assert order == ["migrate", "serve"]


def test_migrate_prints_what_it_applied(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _record_migrations(env, ["0001_capture"])
    assert cli.main(["migrate"]) == 0
    assert calls == ["postgresql://x"]
    assert json.loads(capsys.readouterr().out) == {"applied": ["0001_capture"]}


def test_an_edited_migration_stops_the_command(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _record_migrations(env, MigrationError("migration 0001_capture changed after it was applied"))
    assert cli.main(["migrate"]) == 1
    assert "0001_capture changed" in capsys.readouterr().err


def test_worker_runs_until_stopped(env: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    async def fake_worker(settings: Any, stop: Any) -> None:
        ran.append(settings.database_url)

    env.setattr(cli, "run_worker", fake_worker)
    assert cli.main(["worker"]) == 0
    assert ran == ["postgresql://x"]


def test_commands_fail_clearly_without_a_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TRIMSET_DATABASE_URL", raising=False)
    assert cli.main(["serve"]) == 2
    assert "TRIMSET_DATABASE_URL is not set" in capsys.readouterr().err

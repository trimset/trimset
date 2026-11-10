from typing import Any

import pytest

from trimset_server import cli


def test_serve_runs_uvicorn_with_the_app_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    monkeypatch.setenv("TRIMSET_DATABASE_URL", "postgresql://x")

    assert cli.main(["serve", "--port", "9000", "--workers", "2"]) == 0

    (call,) = calls
    assert call["app"] == "trimset_server.app:create_app"
    assert call["factory"] is True
    assert (call["host"], call["port"], call["workers"]) == ("0.0.0.0", 9000, 2)
    assert call["server_header"] is False


def test_serve_fails_clearly_without_a_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TRIMSET_DATABASE_URL", raising=False)
    assert cli.main(["serve"]) == 2
    assert "TRIMSET_DATABASE_URL is not set" in capsys.readouterr().err

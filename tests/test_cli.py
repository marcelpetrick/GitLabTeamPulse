from __future__ import annotations

import runpy
import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from gitlab_team_pulse import cli, db

from .conftest import FAKE_BASE, fake_client_factory


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    database = tmp_path / "cli.db"
    monkeypatch.setenv("TEAMPULSE_DATABASE_PATH", str(database))
    monkeypatch.setenv("TEAMPULSE_GITLAB_URL", FAKE_BASE)
    monkeypatch.setenv("TEAMPULSE_GITLAB_TOKEN", "fake-token")
    monkeypatch.chdir(tmp_path)  # no stray .env
    return database


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    return calls


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "gitlab-team-pulse" in capsys.readouterr().out


def test_serve_uses_one_worker(env: Path, uvicorn_calls: list[dict[str, Any]]) -> None:
    assert cli.main(["serve", "--port", "9123"]) == 0
    call = uvicorn_calls[0]
    assert call["workers"] == 1
    assert call["port"] == 9123
    assert call["host"] == "127.0.0.1"
    assert isinstance(call["app"], FastAPI)


def test_migrate(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["migrate"]) == 0
    assert "at revision" in capsys.readouterr().out
    engine = db.make_engine(f"sqlite:///{env}")
    assert db.is_migrated(engine)
    engine.dispose()


def test_invalid_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEAMPULSE_TIMEZONE", "Nowhere/Land")
    assert cli.main(["migrate"]) == 2


def test_refresh(
    env: Path,
    fake_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "default_client_factory", fake_client_factory(fake_app))
    assert cli.main(["refresh"]) == 0
    assert "directory: ok" in capsys.readouterr().out
    fake_app.state.down = True
    assert cli.main(["refresh"]) == 1


def test_doctor_all_good(
    env: Path,
    fake_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "default_client_factory", fake_client_factory(fake_app))
    cli.main(["migrate"])
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "administrator" in out
    assert "GraphQL timelogs query works" in out
    assert "fake-token" not in out


def test_doctor_warnings(
    env: Path,
    fake_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "default_client_factory", fake_client_factory(fake_app))
    fake_app.state.admin = False
    fake_app.state.graphql_error = "nope"
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "[WARN] token" in out
    assert "[WARN] timelogs" in out
    assert "needs migration" in out


def test_doctor_failures(
    env: Path,
    fake_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "default_client_factory", fake_client_factory(fake_app))
    fake_app.state.down = True
    assert cli.main(["doctor"]) == 1
    assert "[FAIL] gitlab" in capsys.readouterr().out
    monkeypatch.delenv("TEAMPULSE_GITLAB_TOKEN")
    monkeypatch.delenv("TEAMPULSE_GITLAB_URL")
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "token missing" in out
    assert "not set" in out


def test_doctor_client_factory_failure(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from gitlab_team_pulse.sync import GitLabNotConfiguredError

    def broken(_settings: object) -> None:
        raise GitLabNotConfiguredError("bad config")

    settings = cli._load_settings()
    checks = cli.run_doctor(settings, broken)  # type: ignore[arg-type]
    assert ("fail", "gitlab", "bad config") in checks


def test_doctor_unreadable_database(env: Path, fake_app: FastAPI) -> None:
    env.mkdir()
    checks = cli.run_doctor(cli._load_settings(), fake_client_factory(fake_app))
    assert any(level == "fail" and area == "database" for level, area, _ in checks)


def test_fake_gitlab_command(uvicorn_calls: list[dict[str, Any]]) -> None:
    assert cli.main(["fake-gitlab", "--port", "9124"]) == 0
    assert uvicorn_calls[0]["port"] == 9124


def test_demo_seeds_selection_against_real_http(
    tmp_path: Path, uvicorn_calls: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    port = free_port()
    database = tmp_path / "demo.db"
    argv = ["demo", "--gitlab-port", str(port), "--database", str(database), "--select", "3"]
    assert cli.main(argv) == 0
    assert "demo dashboard" in capsys.readouterr().out
    engine = db.make_engine(f"sqlite:///{database}")
    from gitlab_team_pulse import store

    with db.make_session_factory(engine)() as session:
        assert store.user_counts(session) == (32, 3)
    engine.dispose()
    settings = cli.demo_settings(cli.build_parser().parse_args(["demo"]))
    assert settings.database_path.name == "demo.db"


def test_module_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["gitlab-team-pulse", "--version"])
    with pytest.raises(SystemExit):
        runpy.run_module("gitlab_team_pulse", run_name="__main__")


def test_demo_database_follows_configured_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEAMPULSE_DATABASE_PATH", "/data/teampulse.db")
    settings = cli.demo_settings(cli.build_parser().parse_args(["demo"]))
    assert settings.database_path == Path("/data/demo.db")


def test_cli_overrides_are_validated(env: Path, uvicorn_calls: list[dict[str, Any]]) -> None:
    assert cli.main(["serve", "--port", "99999"]) == 2
    assert uvicorn_calls == []
    assert cli.main(["serve", "--host", "127.0.0.2", "--port", "8123"]) == 0
    assert (uvicorn_calls[0]["host"], uvicorn_calls[0]["port"]) == ("127.0.0.2", 8123)

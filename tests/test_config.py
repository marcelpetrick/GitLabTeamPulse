from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from gitlab_team_pulse import config
from gitlab_team_pulse.config import Settings, default_database_path


def make(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_defaults_are_sensible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "DEFAULT_SECRET_FILE", tmp_path / "missing")
    settings = make()
    assert settings.selected_refresh_interval_seconds == 600
    assert settings.user_refresh_interval_seconds == 3600
    assert settings.activity_days == 7
    assert settings.gitlab_configured is False
    assert settings.tls_verify is True
    assert settings.database_url.startswith("sqlite:///")


def test_default_database_path_respects_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/srv/data")
    assert default_database_path() == Path("/srv/data/gitlab-team-pulse/teampulse.db")
    monkeypatch.delenv("XDG_DATA_HOME")
    assert default_database_path().parts[-3:] == ("share", "gitlab-team-pulse", "teampulse.db")


def test_env_prefix_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMPULSE_GITLAB_URL", "https://gitlab.example.com/")
    monkeypatch.setenv("TEAMPULSE_LOG_LEVEL", "debug")
    settings = make()
    assert settings.gitlab_url == "https://gitlab.example.com"
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    ("field", "value"),
    [("gitlab_url", "gitlab.example.com"), ("log_level", "LOUD"), ("timezone", "Mars/Base")],
)
def test_invalid_values_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        make(**{field: value})


def test_timezone_object() -> None:
    assert make(timezone="Europe/Berlin").tz.key == "Europe/Berlin"


def test_token_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "DEFAULT_SECRET_FILE", tmp_path / "missing")
    settings = make(gitlab_url="https://g.example", gitlab_token=SecretStr("  abc  "))
    token = settings.resolve_token()
    assert token is not None
    assert token.get_secret_value() == "abc"
    assert settings.gitlab_configured is True
    assert "abc" not in repr(settings)


def test_blank_token_counts_as_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "DEFAULT_SECRET_FILE", tmp_path / "missing")
    assert make(gitlab_token=SecretStr("   ")).resolve_token() is None


def test_secret_file_wins_over_environment(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("from-file\n", encoding="utf-8")
    settings = make(gitlab_token=SecretStr("from-env"), gitlab_token_file=secret)
    token = settings.resolve_token()
    assert token is not None
    assert token.get_secret_value() == "from-file"


def test_empty_secret_file_falls_back(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("\n", encoding="utf-8")
    settings = make(gitlab_token=SecretStr("from-env"), gitlab_token_file=secret)
    token = settings.resolve_token()
    assert token is not None
    assert token.get_secret_value() == "from-env"


def test_default_docker_secret_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "gitlab_token"
    secret.write_text("docker-secret", encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULT_SECRET_FILE", secret)
    token = make().resolve_token()
    assert token is not None
    assert token.get_secret_value() == "docker-secret"


def test_ca_bundle_overrides_verify_flag(tmp_path: Path) -> None:
    bundle = tmp_path / "ca.pem"
    assert make(gitlab_ca_bundle=bundle).tls_verify == str(bundle)
    assert make(gitlab_verify_tls=False).tls_verify is False

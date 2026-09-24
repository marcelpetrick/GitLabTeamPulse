from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import config
from gitlab_team_pulse.db import make_engine, make_session_factory, migrate


@pytest.fixture(autouse=True)
def _no_docker_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never pick up a real /run/secrets token or a developer .env during tests."""
    monkeypatch.setattr(config, "DEFAULT_SECRET_FILE", tmp_path / "no-secret")
    for name in list(__import__("os").environ):
        if name.startswith("TEAMPULSE_"):
            monkeypatch.delenv(name)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    migrate(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)

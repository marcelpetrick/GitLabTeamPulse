from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import config
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.db import make_engine, make_session_factory, migrate
from gitlab_team_pulse.fake_gitlab import FAKE_TOKEN, build_data, create_fake_gitlab
from gitlab_team_pulse.gitlab.client import GitLabClient
from gitlab_team_pulse.sync import SyncService


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


FAKE_BASE = "http://fake.gitlab"


async def _no_sleep(_: float) -> None:
    return None


def fake_client_factory(fake_app: FastAPI) -> Callable[[Settings], GitLabClient]:
    def factory(_settings: Settings) -> GitLabClient:
        return GitLabClient(
            FAKE_BASE,
            SecretStr(FAKE_TOKEN),
            transport=httpx.ASGITransport(app=fake_app),
            sleep=_no_sleep,
            max_retries=1,
        )

    return factory


class Clock:
    """Controllable clock for sync and retention tests."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime.now(UTC).replace(microsecond=0))


@pytest.fixture
def fake_app(clock: Clock) -> FastAPI:
    return create_fake_gitlab(build_data(clock.now))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        gitlab_url=FAKE_BASE,
        gitlab_token=SecretStr(FAKE_TOKEN),
        database_path=tmp_path / "test.db",
    )


@pytest.fixture
async def service(
    settings: Settings, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> AsyncIterator[SyncService]:
    svc = SyncService(
        settings, session_factory, client_factory=fake_client_factory(fake_app), clock=clock
    )
    yield svc
    await svc.aclose()

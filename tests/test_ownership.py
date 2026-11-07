from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from gitlab_team_pulse import app as app_module
from gitlab_team_pulse.app import create_app, wait_for_ownership
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.models import ErrorRecord
from gitlab_team_pulse.ownership import OwnerLock, lock_path

from .conftest import fake_client_factory


def test_lock_is_exclusive_and_reentrant(tmp_path: Path) -> None:
    path = lock_path(tmp_path / "sub" / "pulse.db")
    assert path.name == "pulse.db.lock"
    first, second = OwnerLock(path), OwnerLock(path)
    assert first.acquire()
    assert first.held
    assert first.acquire()  # already held
    assert not second.acquire()
    assert not second.held
    first.release()
    first.release()  # releasing twice is harmless
    assert second.acquire()
    second.release()


def open_owner_problems(app: FastAPI) -> list[ErrorRecord]:
    with app.state.ctx.session_factory() as session:
        return list(
            session.scalars(
                select(ErrorRecord).where(
                    ErrorRecord.operation == "owner", ErrorRecord.resolved_at.is_(None)
                )
            )
        )


def test_a_second_app_on_the_same_database_does_not_crawl(
    settings: Settings, fake_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "OWNER_RETRY_SECONDS", 3600.0)
    first = create_app(settings, client_factory=fake_client_factory(fake_app))
    second = create_app(settings, client_factory=fake_client_factory(fake_app))
    with TestClient(first):
        with TestClient(second):
            assert first.state.ctx.scheduler._task is not None
            assert second.state.ctx.scheduler._task is None
            [problem] = open_owner_problems(second)
            assert problem.severity == "warning"
        # The waiting process stopped without taking over: its warning goes with it.
        assert open_owner_problems(first) == []
    # The owner stopped: a new owner takes over and closes the diagnostic.
    third = create_app(settings, client_factory=fake_client_factory(fake_app))
    with TestClient(third):
        assert third.state.ctx.scheduler._task is not None
        assert open_owner_problems(third) == []


async def test_waiting_process_takes_over_when_the_owner_stops(tmp_path: Path) -> None:
    path = lock_path(tmp_path / "pulse.db")
    owner, waiter = OwnerLock(path), OwnerLock(path)
    assert owner.acquire()
    taken: list[bool] = []

    class Service:
        def resolve_problem(self, subsystem: str, operation: str) -> None:
            taken.append(subsystem == "scheduler" and operation == "owner")

    class Scheduler:
        def start(self) -> None:
            taken.append(True)

    task = asyncio.create_task(
        wait_for_ownership(waiter, Service(), Scheduler(), retry_seconds=0.01)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.05)
    assert not task.done()
    owner.release()
    await asyncio.wait_for(task, 2)
    assert taken == [True, True]
    assert waiter.held
    waiter.release()

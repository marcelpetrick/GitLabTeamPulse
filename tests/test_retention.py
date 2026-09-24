from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import retention, store
from gitlab_team_pulse.fake_gitlab import add_event
from gitlab_team_pulse.models import (
    ActivityEventRecord,
    ErrorRecord,
    Project,
    SyncRun,
    SyncState,
    TimelogRecord,
    User,
    WorkItemAssignee,
    WorkItemRecord,
)
from gitlab_team_pulse.sync import SyncService

from .conftest import Clock

ALEX = 2


def count(session: Session, model: type, **where: object) -> int:
    statement = select(func.count()).select_from(model)
    for name, value in where.items():
        statement = statement.where(getattr(model, name) == value)
    return int(session.scalar(statement) or 0)


async def prepare(service: SyncService, session_factory: sessionmaker[Session], *ids: int) -> None:
    await service.sync_directory("startup")
    with session_factory() as session:
        for uid in ids:
            store.set_selected(session, uid, True, service.clock())
        session.commit()
    assert await service.sync_selected("scheduled") == "ok"


async def test_critical_resilience_scenario(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    """VISION 37.3: success, outage beyond retention, cleanup + refresh, data still there."""
    await prepare(service, session_factory, ALEX)
    with session_factory() as session:
        snapshot = (
            count(session, WorkItemAssignee, user_id=ALEX),
            count(session, ActivityEventRecord, user_id=ALEX),
            count(session, TimelogRecord, user_id=ALEX),
            count(session, Project),
        )
    assert all(snapshot)

    clock.advance(days=12)  # far beyond the 1-day retention and the 7-day window
    fake_app.state.down = True
    assert service.cleanup() == "ok"
    assert await service.sync_selected("scheduled") == "error"
    assert service.cleanup() == "ok"

    with session_factory() as session:
        assert (
            count(session, WorkItemAssignee, user_id=ALEX),
            count(session, ActivityEventRecord, user_id=ALEX),
            count(session, TimelogRecord, user_id=ALEX),
            count(session, Project),
        ) == snapshot
        user = session.get(User, ALEX)
        assert user is not None
        assert user.selected
        work_state = store.get_state(session, "work", ALEX)
        assert work_state.status == "error"
        assert work_state.last_success_at == clock.now - timedelta(days=12)


async def test_trims_to_window_after_fresh_success(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    for days in range(10, 30):
        add_event(fake_app.state.data, ALEX, clock.now - timedelta(days=days))
    await prepare(service, session_factory, ALEX)
    with session_factory() as session:
        before = count(session, ActivityEventRecord, user_id=ALEX)
    clock.advance(days=5)
    assert await service.sync_selected("scheduled") == "ok"
    with session_factory() as session:
        stats = retention.cleanup(session, service.settings, clock.now)
        session.commit()
        start = retention.window_start(service.settings, clock.now)
        remaining = list(
            session.scalars(select(ActivityEventRecord).where(ActivityEventRecord.user_id == ALEX))
        )
        old = [e for e in remaining if e.occurred_at < start]
        assert stats.events > 0
        assert len(remaining) < before
        assert len(remaining) >= retention.RECENT_EVENTS_KEPT
        newest_twelve = sorted(remaining, key=lambda e: e.occurred_at, reverse=True)[:12]
        assert all(e in newest_twelve for e in old)
        assert all(t.spent_at >= start for t in session.scalars(select(TimelogRecord)))


async def test_deselected_users_expire_after_retention(
    service: SyncService, session_factory: sessionmaker[Session], clock: Clock
) -> None:
    await prepare(service, session_factory, ALEX)
    with session_factory() as session:
        store.set_selected(session, ALEX, False, clock.now)
        session.commit()
        retention.cleanup(session, service.settings, clock.now)
        session.commit()
        assert count(session, ActivityEventRecord, user_id=ALEX) > 0  # short cache survives
    clock.advance(hours=25)
    with session_factory() as session:
        stats = retention.cleanup(session, service.settings, clock.now)
        session.commit()
        assert stats.events > 0
        assert count(session, ActivityEventRecord, user_id=ALEX) == 0
        assert count(session, TimelogRecord, user_id=ALEX) == 0
        assert count(session, WorkItemAssignee, user_id=ALEX) == 0
        assert count(session, WorkItemRecord) == 0
        assert count(session, SyncState, user_id=ALEX) == 0
        assert session.get(User, ALEX) is not None
    clock.advance(hours=25)
    with session_factory() as session:
        retention.cleanup(session, service.settings, clock.now)
        session.commit()
        assert count(session, Project) == 0


async def test_old_runs_and_resolved_errors_expire(
    service: SyncService, session_factory: sessionmaker[Session], clock: Clock
) -> None:
    await service.sync_directory("startup")
    with session_factory() as session:
        store.record_error(session, subsystem="x", operation="y", message="old", now=clock.now)
        store.record_error(session, subsystem="x", operation="z", message="open", now=clock.now)
        session.flush()
        store.resolve_errors(session, subsystem="x", operation="y", now=clock.now)
        session.commit()
    clock.advance(hours=2)
    await service.sync_directory("scheduled")
    clock.advance(days=8)
    with session_factory() as session:
        stats = retention.cleanup(session, service.settings, clock.now)
        session.commit()
        assert stats.runs == 1
        assert count(session, SyncRun) == 1  # the latest run per kind always stays
        assert stats.errors == 1
        assert [e.message for e in session.scalars(select(ErrorRecord))] == ["open"]
        assert "runs=1" in stats.summary()


def test_summary_when_nothing_happened() -> None:
    stats = retention.CleanupStats()
    assert stats.summary() == "nothing to remove"
    stats.kept_last_known_good.append("activity:1")
    assert stats.summary() == "kept_last_known_good=1"


def test_vacuum_only_when_needed(engine: Engine) -> None:
    assert retention.vacuum_if_needed(engine) is False
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE filler (x BLOB)"))
        connection.execute(
            text(
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 400) "
                "INSERT INTO filler SELECT randomblob(4096) FROM n"
            )
        )
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM filler"))
    assert retention.vacuum_if_needed(engine) is True


def test_cleanup_failure_is_recorded(
    service: SyncService, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object) -> None:
        raise OperationalError("DELETE", {}, Exception("locked"))

    monkeypatch.setattr(retention, "cleanup", broken)
    assert service.cleanup() == "error"
    with session_factory() as session:
        errors = store.list_errors(session)
        assert errors[0].subsystem == "cleanup"

from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import store
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.sync import INCOMPLETE_DIRECTORY, SyncService, default_client_factory

from .conftest import Clock


async def test_directory_sync_stores_every_account_type(
    service: SyncService, session_factory: sessionmaker[Session]
) -> None:
    assert await service.sync_directory("startup") == "ok"
    with session_factory() as session:
        users = store.list_users(session)
        assert len(users) == 32
        types = {u.account_type for u in users}
        states = {u.state for u in users}
        assert types == {"human", "bot", "service", "unknown"}
        assert {"active", "blocked", "deactivated"} <= states
        state = store.get_state(session, "directory")
        assert state.status == "ok"
        run = store.latest_run(session, "directory")
        assert run is not None
        assert run.status == "ok"
        assert run.trigger == "startup"
        assert "32 accounts (32 new, 0 updated)" in run.summary
        assert store.unresolved_error_count(session) == 0
        assert store.data_version(session) == 2
    assert service.runtime.running == set()
    assert service.runtime.last_upstream_ok_at is not None


async def test_directory_resync_preserves_selection(
    service: SyncService, session_factory: sessionmaker[Session], clock: Clock
) -> None:
    await service.sync_directory("scheduled")
    with session_factory() as session:
        store.set_selected(session, 5, True, clock.now)
        session.commit()
    clock.advance(hours=1)
    await service.sync_directory("scheduled")
    with session_factory() as session:
        assert [u.id for u in store.selected_users(session)] == [5]
        assert store.user_counts(session) == (32, 1)
        run = store.latest_run(session, "directory")
        assert run is not None
        assert "0 new, 32 updated" in run.summary


async def test_outage_keeps_users_and_records_error(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await service.sync_directory("scheduled")
    fake_app.state.down = True
    clock.advance(hours=1)
    assert await service.sync_directory("scheduled") == "error"
    with session_factory() as session:
        assert store.user_counts(session)[0] == 32
        state = store.get_state(session, "directory")
        assert state.status == "error"
        assert state.last_success_at == clock.now - timedelta(hours=1)
        errors = store.list_errors(session, include_resolved=False)
        assert len(errors) == 1
        assert "503" in errors[0].message
    assert service.runtime.last_upstream_error is not None
    fake_app.state.down = False
    clock.advance(minutes=10)
    assert await service.sync_directory("manual") == "ok"
    with session_factory() as session:
        assert store.unresolved_error_count(session) == 0


async def test_non_admin_token_raises_completeness_warning(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI
) -> None:
    fake_app.state.admin = False
    await service.sync_directory("scheduled")
    with session_factory() as session:
        errors = store.list_errors(session, include_resolved=False)
        assert [(e.severity, e.message) for e in errors] == [("warning", INCOMPLETE_DIRECTORY)]
        assert store.user_counts(session)[0] < 32
    fake_app.state.admin = True
    await service.sync_directory("scheduled")
    with session_factory() as session:
        assert store.unresolved_error_count(session) == 0


async def test_unconfigured_gitlab_is_a_visible_warning(
    settings: Settings, session_factory: sessionmaker[Session], clock: Clock
) -> None:
    unconfigured = settings.model_copy(update={"gitlab_url": "", "gitlab_token": None})
    svc = SyncService(
        unconfigured, session_factory, client_factory=default_client_factory, clock=clock
    )
    assert await svc.sync_directory("startup") == "error"
    with session_factory() as session:
        errors = store.list_errors(session)
        assert errors[0].severity == "warning"
        assert "not configured" in errors[0].message
    await svc.aclose()


async def test_default_client_factory_builds_client(settings: Settings) -> None:
    client = default_client_factory(settings)
    assert client.base_url == settings.gitlab_url
    await client.aclose()


async def test_directory_due(service: SyncService, fake_app: FastAPI, clock: Clock) -> None:
    assert service.directory_due(clock.now)
    await service.sync_directory("startup")
    assert not service.directory_due(clock.now)
    assert service.directory_due(clock.now + timedelta(hours=1))
    fake_app.state.down = True
    await service.sync_directory("scheduled")
    assert service.directory_due(clock.now + timedelta(minutes=10))

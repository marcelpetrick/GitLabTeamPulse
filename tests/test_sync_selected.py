from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import store
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.fake_gitlab import add_event
from gitlab_team_pulse.models import (
    ActivityEventRecord,
    Project,
    TimelogRecord,
    WorkItemAssignee,
    WorkItemRecord,
)
from gitlab_team_pulse.sync import SyncService, default_client_factory

from .conftest import Clock

ALEX = 2  # first human in the fake instance, with work, events and timelogs


async def select_users(
    service: SyncService, session_factory: sessionmaker[Session], *ids: int
) -> None:
    await service.sync_directory("startup")
    with session_factory() as session:
        for uid in ids:
            store.set_selected(session, uid, True, service.clock())
        session.commit()


def count(session: Session, model: type, **where: object) -> int:
    statement = select(func.count()).select_from(model)
    for name, value in where.items():
        statement = statement.where(getattr(model, name) == value)
    return int(session.scalar(statement) or 0)


async def test_selected_sync_fetches_all_categories(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI
) -> None:
    await select_users(service, session_factory, ALEX, 3)
    assert await service.sync_selected("manual") == "ok"
    data = fake_app.state.data
    with session_factory() as session:
        expected_work = [i for i in data.issues if i["assignees"][0]["id"] == ALEX]
        assert count(session, WorkItemAssignee, user_id=ALEX, relation="assignee") >= len(
            expected_work
        )
        states = {r.state for r in session.scalars(select(WorkItemRecord))}
        assert "closed" in states or "merged" in states  # not open-only
        assert count(session, ActivityEventRecord, user_id=ALEX) > 0
        assert count(session, TimelogRecord, user_id=ALEX) > 0
        assert count(session, Project) > 0
        for category in ("work", "activity", "timelogs"):
            state = store.get_state(session, category, ALEX)
            assert state.status == "ok"
        run = store.latest_run(session, "selected")
        assert run is not None
        assert run.users_attempted == 2
        assert run.users_succeeded == 2
        assert run.status == "ok"
        global_state = store.get_state(session, "selected")
        assert global_state.status == "ok"
        assert global_state.last_success_at is not None


async def test_only_selected_users_are_crawled(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI
) -> None:
    await select_users(service, session_factory, ALEX)
    before = fake_app.state.requests
    await service.sync_selected("scheduled")
    requests = fake_app.state.requests - before
    # work (3 lists) + epics per top-level group (4) + activity + timelogs + a few projects
    assert requests <= 5 + 4 + 6
    with session_factory() as session:
        assert count(session, ActivityEventRecord, user_id=3) == 0


async def test_activity_is_incremental(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await select_users(service, session_factory, ALEX)
    await service.sync_selected("scheduled")
    with session_factory() as session:
        before = count(session, ActivityEventRecord, user_id=ALEX)
        cursor = store.get_state(session, "activity", ALEX).cursor
    assert cursor is not None
    clock.advance(minutes=10)
    add_event(fake_app.state.data, ALEX, clock.now)
    await service.sync_selected("scheduled")
    with session_factory() as session:
        assert count(session, ActivityEventRecord, user_id=ALEX) == before + 1
        new_cursor = store.get_state(session, "activity", ALEX).cursor
    assert new_cursor is not None
    assert datetime.fromisoformat(new_cursor) == clock.now


async def test_quiet_user_still_gets_twelve_events(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    data = fake_app.state.data
    quiet = 4
    data.events = [e for e in data.events if e["author_id"] != quiet]
    for days in range(20, 40):
        add_event(data, quiet, clock.now - timedelta(days=days))
    await select_users(service, session_factory, quiet)
    await service.sync_selected("scheduled")
    with session_factory() as session:
        assert count(session, ActivityEventRecord, user_id=quiet) == 12


async def test_newly_selected_subset_run_leaves_global_state(
    service: SyncService, session_factory: sessionmaker[Session]
) -> None:
    await select_users(service, session_factory, ALEX, 3)
    assert await service.sync_selected("selection", [3]) == "ok"
    with session_factory() as session:
        assert store.find_state(session, "selected") is None
        assert store.find_state(session, "work", ALEX) is None
        assert store.get_state(session, "work", 3).status == "ok"


async def test_no_selected_users_is_cheap(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI
) -> None:
    before = fake_app.state.requests
    assert await service.sync_selected("scheduled") == "ok"
    assert fake_app.state.requests == before


async def test_outage_keeps_last_known_good(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await select_users(service, session_factory, ALEX)
    await service.sync_selected("scheduled")
    with session_factory() as session:
        snapshot = (
            count(session, WorkItemAssignee, user_id=ALEX),
            count(session, ActivityEventRecord, user_id=ALEX),
            count(session, TimelogRecord, user_id=ALEX),
        )
        good = store.get_state(session, "work", ALEX).last_success_at
    fake_app.state.down = True
    clock.advance(minutes=10)
    assert await service.sync_selected("manual") == "error"
    with session_factory() as session:
        assert (
            count(session, WorkItemAssignee, user_id=ALEX),
            count(session, ActivityEventRecord, user_id=ALEX),
            count(session, TimelogRecord, user_id=ALEX),
        ) == snapshot
        work_state = store.get_state(session, "work", ALEX)
        assert work_state.status == "error"
        assert work_state.last_success_at == good
        global_state = store.get_state(session, "selected")
        assert global_state.status == "error"
        assert global_state.last_success_at == good
        assert store.unresolved_error_count(session) == 3


async def test_partial_failure_is_per_category(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI
) -> None:
    await select_users(service, session_factory, ALEX)

    fake_app.state.graphql_error = "timelogs require admin access"
    assert await service.sync_selected("scheduled") == "partial"
    with session_factory() as session:
        assert store.get_state(session, "work", ALEX).status == "ok"
        assert store.get_state(session, "activity", ALEX).status == "ok"
        timelogs = store.get_state(session, "timelogs", ALEX)
        assert timelogs.status == "error"
        assert "timelogs require admin" in (timelogs.last_error or "")
        assert store.get_state(session, "selected").status == "partial"


async def test_unexpected_exception_is_contained(
    service: SyncService, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    await select_users(service, session_factory, ALEX)

    async def explode(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("bug")

    monkeypatch.setattr(service.client(), "get_user_recent_activity", explode)
    assert await service.sync_selected("scheduled") == "partial"
    with session_factory() as session:
        state = store.get_state(session, "activity", ALEX)
        assert "internal error" in (state.last_error or "")


async def test_database_error_during_write_is_contained(
    service: SyncService, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    await select_users(service, session_factory, ALEX)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("INSERT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(store, "replace_user_timelogs", broken)
    assert await service.sync_selected("scheduled") == "partial"
    with session_factory() as session:
        errors = store.list_errors(session)
        assert any(e.subsystem == "database" for e in errors)


async def test_unconfigured_selected_sync(
    settings: Settings, session_factory: sessionmaker[Session], clock: Clock
) -> None:
    unconfigured = settings.model_copy(update={"gitlab_url": ""})
    svc = SyncService(
        unconfigured, session_factory, client_factory=default_client_factory, clock=clock
    )
    with session_factory() as session:
        store.upsert_users(session, [], clock.now)
        from gitlab_team_pulse.models import User

        session.add(
            User(
                id=9,
                username="x",
                name="X",
                selected=True,
                first_seen_at=clock.now,
                last_seen_at=clock.now,
            )
        )
        session.commit()
    assert await svc.sync_selected("scheduled") == "error"
    with session_factory() as session:
        assert store.get_state(session, "selected").status == "error"
        assert any("not configured" in e.message for e in store.list_errors(session))


async def test_project_metadata_failures_are_tolerated(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI
) -> None:
    fake_app.state.data.projects.pop(1)
    fake_app.state.data.issues = [i for i in fake_app.state.data.issues if i["project_id"] != 1]
    fake_app.state.data.merge_requests = [
        m for m in fake_app.state.data.merge_requests if m["project_id"] != 1
    ]
    fake_app.state.data.timelogs = [
        t for t in fake_app.state.data.timelogs if t["issue"]["project_id"] != 1
    ]
    await select_users(service, session_factory, ALEX)
    assert await service.sync_selected("scheduled") == "ok"
    with session_factory() as session:
        assert session.get(Project, 1) is None
        assert count(session, Project) > 0


async def test_projects_are_not_refetched_while_fresh(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await select_users(service, session_factory, ALEX)
    await service.sync_selected("scheduled")
    before = fake_app.state.requests
    clock.advance(minutes=10)
    await service.sync_selected("scheduled")
    assert 5 <= fake_app.state.requests - before <= 5 + 4  # no project metadata requests


async def test_work_snapshot_drops_unassigned_items(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await select_users(service, session_factory, ALEX)
    await service.sync_selected("scheduled")
    data = fake_app.state.data
    moved = next(i for i in data.issues if i["assignees"][0]["id"] == ALEX)
    moved["assignees"] = [{"id": 3, "username": "someone"}]
    clock.advance(minutes=10)
    await service.sync_selected("scheduled")
    with session_factory() as session:
        gone = session.scalar(select(WorkItemRecord).where(WorkItemRecord.gitlab_id == moved["id"]))
        assert gone is None


def test_timelog_window_is_seven_local_days(service: SyncService) -> None:
    berlin = service.settings.model_copy(update={"timezone": "Europe/Berlin"})
    service.settings = berlin
    now = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    assert service.timelog_window_start(now) == datetime(2026, 9, 17, 22, 0, tzinfo=UTC)


def test_selected_due(
    service: SyncService, session_factory: sessionmaker[Session], clock: Clock
) -> None:
    assert service.selected_due(clock.now)
    assert service.selected_next_due(clock.now) is None
    with session_factory() as session:
        state = store.get_state(session, "selected")
        state.last_attempt_at = clock.now
        session.commit()
    assert not service.selected_due(clock.now)
    assert service.selected_due(clock.now + timedelta(minutes=10))


async def test_epics_are_included_when_available(
    service: SyncService, session_factory: sessionmaker[Session]
) -> None:
    await select_users(service, session_factory, ALEX)
    assert await service.sync_selected("scheduled") == "ok"
    assert service.runtime.epics == "available"
    with session_factory() as session:
        epics = list(session.scalars(select(WorkItemRecord).where(WorkItemRecord.kind == "epic")))
        assert [e.reference for e in epics] == ["platform&1"]
        assert epics[0].priority == "high"
        assert epics[0].project_id is None


async def test_missing_epic_capability_is_a_diagnostic_not_a_failure(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    fake_app.state.epics_supported = False
    await select_users(service, session_factory, ALEX)
    assert await service.sync_selected("scheduled") == "ok"
    assert service.runtime.epics == "unavailable"
    with session_factory() as session:
        errors = store.list_errors(session, include_resolved=False)
        assert [(e.operation, e.severity) for e in errors] == [("epics", "warning")]
        assert store.get_state(session, "work", ALEX).status == "ok"
    before = fake_app.state.requests
    clock.advance(minutes=10)
    await service.sync_selected("scheduled")
    assert fake_app.state.requests - before == 5  # no epic query until the recheck interval
    fake_app.state.epics_supported = True
    clock.advance(hours=1)
    await service.sync_selected("scheduled")
    assert service.runtime.epics == "available"
    with session_factory() as session:
        assert store.unresolved_error_count(session) == 0


async def test_failed_project_lookups_are_not_repeated_every_run(
    service: SyncService,
    session_factory: sessionmaker[Session],
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gitlab_team_pulse.gitlab.errors import GitLabForbiddenError

    client = service.client()
    real_get_project = client.get_project
    lookups: list[int] = []

    async def get_project(project_id: int):  # type: ignore[no-untyped-def]
        lookups.append(project_id)
        if project_id == 1:
            raise GitLabForbiddenError("forbidden (403)", status=403)
        return await real_get_project(project_id)

    monkeypatch.setattr(client, "get_project", get_project)
    await select_users(service, session_factory, ALEX)
    await service.sync_selected("scheduled")
    assert 1 in lookups
    first_failure = service._project_failures[1]
    lookups.clear()
    clock.advance(minutes=10)
    await service.sync_selected("scheduled")
    assert 1 not in lookups  # not requested again within the retention period
    assert service._project_failures[1] == first_failure
    clock.advance(hours=25)
    await service.sync_selected("scheduled")
    assert 1 in lookups  # retried after the retention period
    assert service._project_failures[1] == clock.now


async def test_transient_graphql_error_keeps_last_known_good_epics(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await select_users(service, session_factory, ALEX)
    await service.sync_selected("scheduled")

    def epic_count() -> int:
        with session_factory() as session:
            return count(session, WorkItemRecord, kind="epic")

    assert epic_count() == 1
    fake_app.state.graphql_error = "Internal server error"  # transient, not a capability gap
    clock.advance(minutes=10)
    assert await service.sync_selected("scheduled") == "partial"
    assert epic_count() == 1
    assert service.runtime.epics == "available"
    with session_factory() as session:
        assert store.get_state(session, "work", ALEX).status == "error"


async def test_epics_that_worked_before_are_not_dropped_on_capability_errors(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await select_users(service, session_factory, ALEX)
    await service.sync_selected("scheduled")
    fake_app.state.epics_supported = False
    clock.advance(minutes=10)
    await service.sync_selected("scheduled")
    with session_factory() as session:
        assert count(session, WorkItemRecord, kind="epic") == 1
        assert store.get_state(session, "work", ALEX).status == "error"

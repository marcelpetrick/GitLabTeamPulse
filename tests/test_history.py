"""History sync (daily aggregates, outcomes, milestone progress), team members and retention."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import retention, store
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.gitlab.models import ActivityEvent, WorkItem, history_metric
from gitlab_team_pulse.models import (
    ActivityDaily,
    ActivityEventRecord,
    Milestone,
    Outcome,
    SyncState,
    TimelogDaily,
    WorkItemAssignee,
    WorkItemRecord,
)
from gitlab_team_pulse.sync import SyncService

from .conftest import Clock

ALEX, SAM, ROBIN = 2, 3, 4


def count(session: Session, model: type, **where: object) -> int:
    statement = select(func.count()).select_from(model)
    for name, value in where.items():
        statement = statement.where(getattr(model, name) == value)
    return int(session.scalar(statement) or 0)


async def prepare(
    service: SyncService,
    session_factory: sessionmaker[Session],
    *,
    selected: tuple[int, ...] = (ALEX,),
    team: tuple[int, ...] = (),
) -> None:
    await service.sync_directory("startup")
    with session_factory() as session:
        for uid in selected:
            store.set_selected(session, uid, True, service.clock())
        if team:
            store.save_team(
                session,
                None,
                name="Team",
                description="",
                member_ids=list(team),
                scopes=[],
                now=service.clock(),
            )
        session.commit()


async def test_history_is_loaded_for_selected_users(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI
) -> None:
    await prepare(service, session_factory)
    assert await service.sync_selected("scheduled") == "ok"
    data = fake_app.state.data
    with session_factory() as session:
        metrics = set(session.scalars(select(ActivityDaily.metric).distinct()))
        assert metrics <= {
            "mr_opened",
            "mr_merged",
            "mr_approved",
            "mr_commented",
            "issue_opened",
            "issue_closed",
            "issue_commented",
        }
        assert {"mr_approved", "mr_commented"} <= metrics
        logged = session.scalar(select(func.sum(TimelogDaily.seconds))) or 0
        expected = sum(t["seconds"] for t in data.timelogs if t["user_id"] == ALEX)
        assert logged == expected  # half a year, not just the last week
        merged = {
            m["id"]
            for m in data.merge_requests
            if m["state"] == "merged" and m["author"]["username"] == "alex.berger"
        }
        stored = set(
            session.scalars(select(Outcome.gitlab_id).where(Outcome.kind == "merge_request"))
        )
        assert stored == merged
        assert count(session, Outcome, kind="issue") > 0
        for category in ("timelog_history", "outcomes"):
            assert store.get_state(session, category, ALEX).status == "ok"
        stats = [m.stats for m in session.scalars(select(Milestone)) if m.stats]
        assert stats
        assert all(s["total"] >= s["closed"] for s in stats)
        assert store.get_state(session, "milestones").status == "ok"


async def test_team_members_get_history_but_no_dashboard_data(
    service: SyncService,
    session_factory: sessionmaker[Session],
    fake_app: FastAPI,
    clock: Clock,
) -> None:
    await prepare(service, session_factory, team=(ALEX, SAM))
    fake_app.state.request_log.clear()
    assert await service.sync_selected("scheduled") == "ok"
    with session_factory() as session:
        assert count(session, Outcome, user_id=SAM) > 0
        assert count(session, TimelogDaily, user_id=SAM) > 0
        assert count(session, WorkItemAssignee, user_id=SAM) == 0
        assert count(session, ActivityEventRecord, user_id=SAM) == 0
        assert store.find_state(session, "work", SAM) is None
        run = store.latest_run(session, "selected")
        assert run is not None
        assert run.users_attempted == 2
    # Ten minutes later only the dashboard data of the selected user is refreshed.
    clock.advance(minutes=10)
    fake_app.state.request_log.clear()
    await service.sync_selected("scheduled")
    sam = [q for p, q in fake_app.state.request_log if f"={SAM}" in q or f"/{SAM}/" in p]
    assert sam == []
    with session_factory() as session:
        run = store.latest_run(session, "selected")
        assert run is not None
        assert run.users_attempted == 1


async def test_tracked_team_members_are_capped(
    service: SyncService, session_factory: sessionmaker[Session]
) -> None:
    service.settings = service.settings.model_copy(update={"max_tracked_users": 2})
    await prepare(service, session_factory, team=(SAM, ROBIN, 5))
    selected, team_only = service.tracked_users()
    assert [uid for uid, _ in selected] == [ALEX]
    assert [uid for uid, _ in team_only] == [SAM]
    with session_factory() as session:
        errors = store.list_errors(session, include_resolved=False)
        assert [(e.operation, e.severity) for e in errors] == [("tracked_users", "warning")]
        assert "2 team members" in errors[0].message
    service.settings = service.settings.model_copy(update={"max_tracked_users": 30})
    assert len(service.tracked_users()[1]) == 3
    assert service.tracked_users([ROBIN]) == ([], [(ROBIN, "robin.tanaka")])
    with session_factory() as session:
        assert store.unresolved_error_count(session) == 0


async def test_outcomes_are_incremental_and_drop_reopened_issues(
    service: SyncService,
    session_factory: sessionmaker[Session],
    fake_app: FastAPI,
    clock: Clock,
) -> None:
    await prepare(service, session_factory)
    await service.sync_selected("scheduled")
    data = fake_app.state.data
    closed = next(
        i
        for i in data.issues
        if i["state"] == "closed" and i["assignees"][0]["id"] == ALEX and i["closed_at"]
    )
    with session_factory() as session:
        assert count(session, Outcome, gitlab_id=closed["id"]) == 1
        cursor = store.get_state(session, "outcomes", ALEX).cursor
    closed["state"] = "opened"
    closed["closed_at"] = None
    closed["updated_at"] = clock.advance(hours=2).isoformat()
    fake_app.state.request_log.clear()
    await service.sync_selected("scheduled")
    with session_factory() as session:
        assert count(session, Outcome, gitlab_id=closed["id"]) == 0
    queries = [q for p, q in fake_app.state.request_log if p.endswith("/merge_requests")]
    expected = (datetime.fromisoformat(cursor) - timedelta(days=1)).isoformat()
    assert any(f"author_id={ALEX}" in q and f"updated_after={expected[:10]}" in q for q in queries)


async def test_timelog_history_resweeps_recent_weeks(
    service: SyncService,
    session_factory: sessionmaker[Session],
    fake_app: FastAPI,
    clock: Clock,
) -> None:
    await prepare(service, session_factory)
    await service.sync_selected("scheduled")
    clock.advance(hours=2)
    fake_app.state.request_log.clear()
    await service.sync_selected("scheduled")
    with session_factory() as session:
        cursor = store.get_state(session, "timelog_history", ALEX).cursor
    assert cursor.endswith("@UTC")
    # A changed time zone backfills the whole window again.
    service.settings = service.settings.model_copy(update={"timezone": "Europe/Berlin"})
    clock.advance(hours=2)
    await service.sync_selected("scheduled")
    with session_factory() as session:
        assert store.get_state(session, "timelog_history", ALEX).cursor.endswith("@Europe/Berlin")


async def test_unreadable_milestones_keep_their_last_progress(
    service: SyncService,
    session_factory: sessionmaker[Session],
    fake_app: FastAPI,
    clock: Clock,
) -> None:
    await prepare(service, session_factory)
    await service.sync_selected("scheduled")
    with session_factory() as session:
        before = {m.id: m.stats for m in session.scalars(select(Milestone)) if m.stats}
    assert before
    fake_app.state.data.milestones.clear()  # every milestone lookup now answers 404
    clock.advance(hours=2)
    assert await service.sync_selected("scheduled") == "ok"
    with session_factory() as session:
        after = {m.id: m.stats for m in session.scalars(select(Milestone)) if m.stats}
        assert after == before
        state = store.get_state(session, "milestones")
        assert state.status == "ok"  # hourly cadence kept, not retried on every run
        assert "404" in (state.last_error or "")
        errors = store.list_errors(session, include_resolved=False)
        assert [e.operation for e in errors] == ["milestones"]
    assert service.milestones_due(clock.advance(minutes=10)) is False
    assert service.milestones_due(clock.advance(hours=1)) is True
    service.settings = service.settings.model_copy(update={"milestones_max": 0})
    assert service.milestones_due(clock.advance(hours=2)) is False


async def test_milestone_progress_follows_what_the_dashboard_shows(
    service: SyncService, session_factory: sessionmaker[Session]
) -> None:
    await prepare(service, session_factory)
    await service.sync_selected("scheduled")
    with session_factory() as session:
        shown = store.relevant_milestones(session, [ALEX], service.settings.milestones_max)
        rows = session.execute(
            select(WorkItemAssignee.relation, WorkItemRecord.milestone_id).join(
                WorkItemRecord, WorkItemRecord.id == WorkItemAssignee.work_item_id
            )
        ).all()
    assert shown
    assert [m.id for m in service.milestones_to_refresh()] == [m.id for m in shown]
    assigned = {mid for relation, mid in rows if relation == "assignee"}
    assert {m.id for m in shown} <= assigned


async def test_milestone_group_and_internal_errors_are_reported(
    service: SyncService, session_factory: sessionmaker[Session], clock: Clock
) -> None:
    await prepare(service, session_factory)
    await service.sync_selected("scheduled")
    client = service.client()

    async def broken(milestone_id: int, **_: object) -> list[WorkItem]:
        raise RuntimeError("boom")

    client.get_milestone_issues = broken  # type: ignore[method-assign]
    assert await service.sync_milestones(client, "run") is False
    with session_factory() as session:
        assert "internal error" in (store.get_state(session, "milestones").last_error or "")


async def test_retention_keeps_team_history_and_purges_the_rest(
    service: SyncService,
    session_factory: sessionmaker[Session],
    settings: Settings,
    clock: Clock,
) -> None:
    await prepare(service, session_factory, selected=(ALEX, SAM), team=(SAM,))
    await service.sync_selected("scheduled")
    with session_factory() as session:
        for uid in (ALEX, SAM):
            store.set_selected(session, uid, False, clock.now)
        session.commit()
    later = clock.advance(days=2)
    with session_factory() as session:
        for state in session.scalars(select(SyncState).where(SyncState.user_id == SAM)):
            if state.category in ("contributions", "timelog_history", "outcomes"):
                state.last_success_at = later  # the team keeps Sam's history fresh
        session.commit()
    with session_factory() as session:
        stats = retention.cleanup(session, settings, later)
        session.commit()
        # Alex left the dashboard and is in no team: everything is gone.
        assert count(session, Outcome, user_id=ALEX) == 0
        assert count(session, TimelogDaily, user_id=ALEX) == 0
        assert count(session, SyncState, user_id=ALEX) == 0
        # Sam is still in a team: history stays, the dashboard cache expires.
        assert count(session, Outcome, user_id=SAM) > 0
        assert count(session, ActivityDaily, user_id=SAM) > 0
        assert count(session, WorkItemAssignee, user_id=SAM) == 0
        assert store.find_state(session, "work", SAM) is None
        assert store.find_state(session, "outcomes", SAM) is not None
    assert stats.history_days > 0
    assert stats.outcomes > 0


def test_retention_trims_history_older_than_the_calendar(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    now = datetime(2026, 10, 15, tzinfo=UTC)
    old_day = date(2025, 9, 1)
    with session_factory() as session:
        from gitlab_team_pulse.gitlab.models import GitLabUser

        store.upsert_users(session, [GitLabUser(id=ALEX, username="a", name="A")], now)
        store.set_selected(session, ALEX, True, now)
        session.add_all(
            [
                ActivityDaily(user_id=ALEX, day=old_day, project_id=0, metric="mr_opened", value=1),
                TimelogDaily(user_id=ALEX, day=old_day, project_id=0, seconds=60),
                TimelogDaily(user_id=ALEX, day=date(2026, 10, 1), project_id=0, seconds=60),
                Outcome(
                    user_id=ALEX,
                    kind="issue",
                    gitlab_id=1,
                    iid=1,
                    reference="#1",
                    title="t",
                    finished_at=datetime(2025, 9, 1, tzinfo=UTC),
                ),
            ]
        )
        session.commit()
        stats = retention.cleanup(session, settings, now)
        session.commit()
        assert count(session, ActivityDaily) == 0
        assert count(session, TimelogDaily) == 1
        assert count(session, Outcome) == 0
    assert (stats.history_days, stats.outcomes) == (2, 1)


def event(
    action: str, target: str | None, category: str = "issue", **extra: object
) -> ActivityEvent:
    return ActivityEvent(
        id=1,
        project_id=1,
        action_name=action,
        target_type=target,
        target_title=None,
        category=category,  # type: ignore[arg-type]
        summary="",
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        url_path=None,
        **extra,  # type: ignore[arg-type]
    )


def test_history_metrics() -> None:
    assert history_metric(event("opened", "MergeRequest", "merge_request")) == "mr_opened"
    assert history_metric(event("accepted", "MergeRequest", "merge_request")) == "mr_merged"
    assert history_metric(event("approved", "MergeRequest", "merge_request")) == "mr_approved"
    assert history_metric(event("approved", None, "merge_request")) == "mr_approved"
    assert history_metric(event("closed", "MergeRequest", "merge_request")) is None
    assert history_metric(event("opened", "Issue")) == "issue_opened"
    assert history_metric(event("closed", "WorkItem")) == "issue_closed"
    assert history_metric(event("reopened", "Issue")) is None
    assert history_metric(event("pushed to", None, "push")) is None
    assert (
        history_metric(event("commented on", "Note", "comment", noteable_type="MergeRequest"))
        == "mr_commented"
    )
    assert (
        history_metric(event("commented on", "Note", "comment", noteable_type="Issue"))
        == "issue_commented"
    )
    assert history_metric(event("commented on", "Note", "comment", noteable_type="Commit")) is None


def test_store_outcomes_and_selection_helpers(session_factory: sessionmaker[Session]) -> None:
    now = datetime(2026, 10, 15, tzinfo=UTC)
    with session_factory() as session:
        from gitlab_team_pulse.gitlab.models import GitLabUser

        store.upsert_users(
            session,
            [GitLabUser(id=uid, username=f"u{uid}", name=f"U{uid}") for uid in (1, 2, 3)],
            now,
        )
        store.set_selected(session, 1, True, now)
        assert store.apply_selection(session, {2, 3}, now) == ({2, 3}, 1)
        assert store.apply_selection(session, {2, 3}, now) == (set(), 0)
        store.apply_outcomes(session, 1, [])
        closed_mr = WorkItem(
            kind="merge_request",
            gitlab_id=5,
            iid=5,
            project_id=None,
            reference="!5",
            title="closed, not merged",
            state="closed",
            updated_at=now,
            closed_at=now,
        )
        store.apply_outcomes(session, 1, [closed_mr])
        session.commit()
        assert count(session, Outcome) == 0

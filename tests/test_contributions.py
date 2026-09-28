from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import dashboard, retention, store
from gitlab_team_pulse.gitlab.models import ActivityEvent, counts_as_contribution, parse_event
from gitlab_team_pulse.models import ContributionDay
from gitlab_team_pulse.sync import SyncService, contribution_grid_start

from .conftest import Clock

ALEX = 2


def event(action: str, target: str | None = None, category: str = "other") -> ActivityEvent:
    return ActivityEvent(
        id=1,
        project_id=None,
        action_name=action,
        target_type=target,
        target_title=None,
        category=category,  # type: ignore[arg-type]
        summary="",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        url_path=None,
    )


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (event("pushed to", category="push"), True),
        (event("commented on", "Note", "comment"), True),
        (event("opened", "Issue", "issue"), True),
        (event("closed", "WorkItem", "issue"), True),
        (event("accepted", "MergeRequest", "merge_request"), True),
        (event("approved", "MergeRequest", "merge_request"), True),
        (event("updated", "DesignManagement::Design"), False),
        (event("uploaded", "DesignManagement::Design"), True),  # a created design
        (event("revised", "DesignManagement::Design"), False),
        (event("joined"), False),
        (event("reopened", "Issue", "issue"), False),
        (event("closed", "Milestone"), False),
    ],
)
def test_gitlab_contribution_rule(item: ActivityEvent, expected: bool) -> None:
    assert counts_as_contribution(item) is expected


def test_grid_starts_on_a_sunday_53_weeks_back() -> None:
    for today in (date(2026, 9, 25), date(2026, 9, 27), date(2026, 9, 28)):
        start = contribution_grid_start(today)
        assert start.weekday() == 6
        assert 52 * 7 <= (today - start).days < 53 * 7


def expected_counts(fake_app: FastAPI, start: date) -> dict[date, int]:
    counts: dict[date, int] = {}
    for raw in fake_app.state.data.events:
        if raw["author_id"] != ALEX:
            continue
        parsed = parse_event(raw)
        day = parsed.created_at.date()
        if day >= start and counts_as_contribution(parsed):
            counts[day] = counts.get(day, 0) + 1
    return counts


def stored(session_factory: sessionmaker[Session]) -> dict[date, int]:
    with session_factory() as session:
        rows = session.execute(
            select(ContributionDay.day, ContributionDay.count).where(
                ContributionDay.user_id == ALEX
            )
        ).all()
    return {row.day: row.count for row in rows}


async def select_alex(service: SyncService, session_factory: sessionmaker[Session]) -> None:
    await service.sync_directory("startup")
    with session_factory() as session:
        store.set_selected(session, ALEX, True, service.clock())
        session.commit()


async def test_backfill_matches_gitlab_counting(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await select_alex(service, session_factory)
    assert await service.sync_selected("scheduled") == "ok"
    start = service.contribution_window_start(clock.now)
    expected = expected_counts(fake_app, start)
    assert len(expected) > 100  # a year of history, not just the last week
    assert stored(session_factory) == expected
    with session_factory() as session:
        state = store.get_state(session, "contributions", ALEX)
        assert state.status == "ok"
        assert state.cursor == f"{clock.now.date().isoformat()}@UTC"


async def test_changed_timezone_forces_a_full_backfill(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await select_alex(service, session_factory)
    await service.sync_selected("scheduled")
    service.settings = service.settings.model_copy(update={"timezone": "Europe/Berlin"})
    clock.advance(hours=2)
    fake_app.state.request_log.clear()
    await service.sync_selected("scheduled")
    start = service.contribution_window_start(clock.now)
    backfill_after = (start - timedelta(days=2)).isoformat()
    queries = [q for p, q in fake_app.state.request_log if p.endswith("/events")]
    assert any(f"after={backfill_after}" in q for q in queries)
    with session_factory() as session:
        assert store.get_state(session, "contributions", ALEX).cursor.endswith("@Europe/Berlin")


async def test_legacy_cursor_without_a_zone_stays_incremental(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await select_alex(service, session_factory)
    await service.sync_selected("scheduled")
    with session_factory() as session:
        store.get_state(session, "contributions", ALEX).cursor = clock.now.date().isoformat()
        session.commit()
    clock.advance(hours=2)
    fake_app.state.request_log.clear()
    await service.sync_selected("scheduled")
    incremental_after = (clock.now.date() - timedelta(days=3)).isoformat()
    queries = [q for p, q in fake_app.state.request_log if p.endswith("/events")]
    assert any(f"after={incremental_after}" in q for q in queries)


async def test_refresh_is_throttled_then_incremental(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    from gitlab_team_pulse.fake_gitlab import add_event

    await select_alex(service, session_factory)
    await service.sync_selected("scheduled")
    fake_app.state.request_log.clear()
    clock.advance(minutes=10)
    await service.sync_selected("scheduled")
    event_queries = [q for p, q in fake_app.state.request_log if p.endswith("/events")]
    assert len(event_queries) == 1  # only the activity dataset; the calendar is not due yet

    clock.advance(hours=1)
    add_event(fake_app.state.data, ALEX, clock.now - timedelta(minutes=1))
    fake_app.state.request_log.clear()
    await service.sync_selected("scheduled")
    event_queries = [q for p, q in fake_app.state.request_log if p.endswith("/events")]
    incremental_after = (clock.now.date() - timedelta(days=3)).isoformat()
    assert any(f"after={incremental_after}" in q for q in event_queries)
    start = service.contribution_window_start(clock.now)
    assert stored(session_factory) == expected_counts(fake_app, start)


async def test_failed_refresh_keeps_the_calendar(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await select_alex(service, session_factory)
    await service.sync_selected("scheduled")
    before = stored(session_factory)
    fake_app.state.down = True
    clock.advance(hours=2)
    await service.sync_selected("scheduled")
    assert stored(session_factory) == before
    with session_factory() as session:
        assert store.get_state(session, "contributions", ALEX).status == "error"


async def test_failed_refresh_is_retried_on_the_next_run(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await select_alex(service, session_factory)
    fake_app.state.down = True
    await service.sync_selected("scheduled")
    with session_factory() as session:
        assert store.get_state(session, "contributions", ALEX).status == "error"
    fake_app.state.down = False
    clock.advance(minutes=10)  # well inside the hourly throttle
    await service.sync_selected("scheduled")
    with session_factory() as session:
        assert store.get_state(session, "contributions", ALEX).status == "ok"
    start = service.contribution_window_start(clock.now)
    assert stored(session_factory) == expected_counts(fake_app, start)


async def test_calendar_stays_fresh_until_the_next_run_after_the_throttle(
    service: SyncService, session_factory: sessionmaker[Session], clock: Clock
) -> None:
    await select_alex(service, session_factory)
    await service.sync_selected("scheduled")
    # Due after 60 min, but the refresh only starts on the next selected run (every 10 min).
    clock.advance(minutes=65)
    with session_factory() as session:
        reader = dashboard.DashboardReader(session, service.settings, service.runtime, clock.now)
        card = reader.cards(store.selected_users(session))[0]
    assert card["contributions"]["freshness"]["status"] == "fresh"


async def test_calendar_errors_map_to_the_calendar_state(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    await select_alex(service, session_factory)
    await service.sync_selected("scheduled")
    clock.advance(minutes=10)
    await service.sync_selected("scheduled")  # the calendar is not due: only `selected` moves on
    fake_app.state.down = True
    clock.advance(hours=1)
    await service.sync_selected("scheduled")
    with session_factory() as session:
        calendar_success = store.get_state(session, "contributions", ALEX).last_success_at
        selected_success = store.get_state(session, "selected").last_success_at
        payloads = dashboard.error_payloads(session, limit=50, include_resolved=False)
    assert calendar_success != selected_success
    errors = [p for p in payloads if p["operation"] == "contributions"]
    assert errors
    assert errors[0]["related_dataset"] == store.state_key("contributions", ALEX)
    assert errors[0]["last_success_at"] == calendar_success


async def test_days_follow_the_configured_timezone(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    from gitlab_team_pulse.fake_gitlab import add_event

    service.settings = service.settings.model_copy(update={"timezone": "Pacific/Kiritimati"})
    fake_app.state.data.events = [e for e in fake_app.state.data.events if e["author_id"] != ALEX]
    late_evening_utc = datetime.combine(
        clock.now.date() - timedelta(days=5), datetime.min.time(), UTC
    ) + timedelta(hours=20)
    pushed = add_event(fake_app.state.data, ALEX, late_evening_utc)
    pushed.update(action_name="pushed to", push_data={"commit_count": 1, "ref": "main"})
    await select_alex(service, session_factory)
    await service.sync_selected("scheduled")
    assert stored(session_factory) == {late_evening_utc.date() + timedelta(days=1): 1}  # UTC+14


async def test_payload_shape_and_retention(
    service: SyncService, session_factory: sessionmaker[Session], clock: Clock
) -> None:
    await select_alex(service, session_factory)
    await service.sync_selected("scheduled")
    with session_factory() as session:
        reader = dashboard.DashboardReader(session, service.settings, service.runtime, clock.now)
        card = reader.cards(store.selected_users(session))[0]
    calendar = card["contributions"]
    assert calendar["start"].weekday() == 6
    assert calendar["end"] == clock.now.date()
    assert len(calendar["counts"]) == (calendar["end"] - calendar["start"]).days + 1
    assert calendar["total"] == sum(calendar["counts"]) == sum(stored(session_factory).values())
    assert calendar["busiest"]["count"] == calendar["max"] > 0
    assert calendar["freshness"]["status"] == "fresh"

    with session_factory() as session:
        session.add(
            ContributionDay(user_id=ALEX, day=clock.now.date() - timedelta(days=400), count=3)
        )
        session.commit()
        stats = retention.cleanup(session, service.settings, clock.now)
        session.commit()
        assert stats.contribution_days == 1
        store.set_selected(session, ALEX, False, clock.now)
        session.commit()
    clock.advance(hours=25)
    with session_factory() as session:
        retention.cleanup(session, service.settings, clock.now)
        session.commit()
        assert session.scalar(select(func.count()).select_from(ContributionDay)) == 0


def test_empty_calendar_payload(session_factory: sessionmaker[Session], settings: object) -> None:
    from gitlab_team_pulse.models import User
    from gitlab_team_pulse.sync import RuntimeState

    now = datetime(2026, 9, 25, 12, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            User(id=9, username="z", name="Z", selected=True, first_seen_at=now, last_seen_at=now)
        )
        session.commit()
        reader = dashboard.DashboardReader(session, settings, RuntimeState(), now)  # type: ignore[arg-type]
        calendar = reader.cards(store.selected_users(session))[0]["contributions"]
    assert calendar["total"] == 0
    assert calendar["busiest"] is None
    assert calendar["freshness"]["status"] == "never"

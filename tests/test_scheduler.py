from __future__ import annotations

import asyncio

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import store
from gitlab_team_pulse.scheduler import Scheduler
from gitlab_team_pulse.sync import SyncService

from .conftest import Clock


async def test_first_tick_runs_startup_jobs(
    service: SyncService, session_factory: sessionmaker[Session]
) -> None:
    scheduler = Scheduler(service)
    await scheduler.tick()
    await scheduler.wait_idle()
    with session_factory() as session:
        directory = store.latest_run(session, "directory")
        selected = store.latest_run(session, "selected")
        assert directory is not None
        assert directory.trigger == "startup"
        assert selected is not None
        assert selected.trigger == "startup"
    assert scheduler.next_selected_due is None  # computed before the job finished
    await scheduler.tick()
    await scheduler.wait_idle()
    assert scheduler.next_selected_due is not None


async def test_nothing_runs_when_not_due(
    service: SyncService, session_factory: sessionmaker[Session], clock: Clock
) -> None:
    scheduler = Scheduler(service)
    await scheduler.tick()
    await scheduler.wait_idle()
    clock.advance(minutes=1)
    await scheduler.tick()
    assert not scheduler.is_running("directory")
    assert not scheduler.is_running("selected")
    assert not scheduler.is_running("cleanup")
    clock.advance(minutes=10)
    await scheduler.tick()
    assert scheduler.is_running("selected")
    assert not scheduler.is_running("directory")
    await scheduler.wait_idle()


async def test_manual_refresh_is_throttled_and_coalesced(
    service: SyncService, session_factory: sessionmaker[Session], clock: Clock
) -> None:
    scheduler = Scheduler(service)
    first = scheduler.request_refresh()
    assert (first.accepted, first.status) == (True, "started")
    assert scheduler.refresh_pending
    second = scheduler.request_refresh()
    assert (second.accepted, second.status) == (True, "coalesced")
    await scheduler.tick()
    assert scheduler.is_running("selected")
    assert scheduler.is_running("directory")  # directory never succeeded yet
    running = scheduler.request_refresh()
    assert running.status == "coalesced"
    await scheduler.wait_idle()
    clock.advance(seconds=3)
    throttled = scheduler.request_refresh()
    assert (throttled.accepted, throttled.status) == (False, "throttled")
    assert throttled.retry_after_seconds == 7.0
    clock.advance(seconds=10)
    assert scheduler.request_refresh().status == "started"
    await scheduler.tick()
    await scheduler.wait_idle()
    with session_factory() as session:
        run = store.latest_run(session, "selected")
        assert run is not None
        assert run.trigger == "manual"


async def test_newly_selected_user_is_synced_immediately(
    service: SyncService, session_factory: sessionmaker[Session], clock: Clock
) -> None:
    scheduler = Scheduler(service)
    await scheduler.tick()
    await scheduler.wait_idle()
    with session_factory() as session:
        store.set_selected(session, 2, True, clock.now)
        session.commit()
    clock.advance(seconds=30)
    scheduler.request_user_sync(2)
    assert scheduler.refresh_pending
    await scheduler.tick()
    await scheduler.wait_idle()
    with session_factory() as session:
        run = store.latest_run(session, "selected")
        assert run is not None
        assert run.trigger == "selection"
        assert store.get_state(session, "work", 2).status == "ok"
    assert not scheduler.refresh_pending


async def test_background_loop_wakes_on_request(
    service: SyncService, session_factory: sessionmaker[Session], fake_app: FastAPI, clock: Clock
) -> None:
    service.settings = service.settings.model_copy(update={"scheduler_tick_seconds": 30})
    scheduler = Scheduler(service)
    scheduler.start()
    scheduler.start()  # idempotent
    for _ in range(100):
        await asyncio.sleep(0.02)
        with session_factory() as session:
            if store.latest_run(session, "directory") and not scheduler.is_running("directory"):
                break
    clock.advance(seconds=20)
    scheduler.request_refresh()
    for _ in range(100):
        await asyncio.sleep(0.02)
        with session_factory() as session:
            run = store.latest_run(session, "selected")
            if run is not None and run.trigger == "manual" and run.status != "running":
                break
    else:  # pragma: no cover - diagnostic only
        raise AssertionError("manual refresh did not run")
    await scheduler.stop()
    assert not scheduler.is_running("selected")


async def test_cleanup_runs_on_interval(service: SyncService, clock: Clock) -> None:
    scheduler = Scheduler(service)
    await scheduler.tick()
    await scheduler.wait_idle()
    clock.advance(hours=1, seconds=1)
    await scheduler.tick()
    assert scheduler.is_running("cleanup")
    await scheduler.wait_idle()

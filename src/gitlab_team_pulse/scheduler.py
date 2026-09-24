"""In-process background scheduler (single owner: run exactly one application worker).

Each tick checks which datasets are due and starts at most one task per job kind. Manual
refreshes and newly selected users wake the loop immediately; requests arriving while a
matching job runs are coalesced into it instead of starting another crawl.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from gitlab_team_pulse.logging_setup import get_logger
from gitlab_team_pulse.sync import SyncService

log = get_logger("scheduler")


@dataclass(frozen=True)
class RefreshResult:
    accepted: bool
    status: str  # started | coalesced | throttled
    retry_after_seconds: float = 0.0


class Scheduler:
    def __init__(self, service: SyncService) -> None:
        self.service = service
        self.settings = service.settings
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._jobs: dict[str, asyncio.Task[object]] = {}
        self._manual_pending = False
        self._pending_users: set[int] = set()
        self._last_manual: datetime | None = None
        self._last_cleanup: datetime | None = None
        self._first_tick = True
        self.next_selected_due: datetime | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="teampulse-scheduler")
            log.info("scheduler started")

    async def stop(self) -> None:
        tasks = [t for t in [self._task, *self._jobs.values()] if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        self._jobs.clear()
        log.info("scheduler stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # pragma: no cover - defensive: the loop must survive
                log.exception("scheduler tick failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.settings.scheduler_tick_seconds)

    # ------------------------------------------------------------------ requests

    def request_refresh(self) -> RefreshResult:
        """Manual "Refresh now": start, coalesce into a running refresh, or throttle storms."""
        now = self.service.clock()
        if self.is_running("selected") or self._manual_pending:
            return RefreshResult(True, "coalesced")
        minimum = self.settings.manual_refresh_min_interval_seconds
        if self._last_manual is not None:
            elapsed = (now - self._last_manual).total_seconds()
            if elapsed < minimum:
                return RefreshResult(False, "throttled", round(minimum - elapsed, 1))
        self._last_manual = now
        self._manual_pending = True
        self._wake.set()
        return RefreshResult(True, "started")

    def request_user_sync(self, user_id: int) -> None:
        """A newly selected user gets synchronized right away, not after up to ten minutes."""
        self._pending_users.add(user_id)
        self._wake.set()

    def is_running(self, kind: str) -> bool:
        task = self._jobs.get(kind)
        return task is not None and not task.done()

    @property
    def refresh_pending(self) -> bool:
        return self._manual_pending or bool(self._pending_users)

    # ------------------------------------------------------------------ tick

    def _spawn(self, kind: str, factory: Callable[[], Awaitable[object]]) -> None:
        async def runner() -> object:
            return await factory()

        self._jobs[kind] = asyncio.create_task(runner(), name=f"teampulse-{kind}")

    async def tick(self) -> None:
        now = self.service.clock()
        trigger = "startup" if self._first_tick else "scheduled"
        self._first_tick = False

        if not self.is_running("directory") and (
            self.service.directory_due(now)
            or (self._manual_pending and not self.service.directory_healthy())
        ):
            self._spawn("directory", lambda: self.service.sync_directory(trigger))

        if not self.is_running("selected"):
            if self._manual_pending:
                self._manual_pending = False
                self._pending_users.clear()
                self._spawn("selected", lambda: self.service.sync_selected("manual"))
            elif self.service.selected_due(now):
                self._pending_users.clear()
                self._spawn("selected", lambda: self.service.sync_selected(trigger))
            elif self._pending_users:
                users = sorted(self._pending_users)
                self._pending_users.clear()
                self._spawn("selected", lambda: self.service.sync_selected("selection", users))
        self.next_selected_due = self.service.selected_next_due(now)

        cleanup_interval = timedelta(seconds=self.settings.cleanup_interval_seconds)
        if not self.is_running("cleanup") and (
            self._last_cleanup is None or now - self._last_cleanup >= cleanup_interval
        ):
            self._last_cleanup = now
            self._spawn("cleanup", lambda: asyncio.to_thread(self.service.cleanup))

    async def wait_idle(self) -> None:
        """Test helper: wait for every spawned job to finish."""
        while running := [t for t in self._jobs.values() if not t.done()]:
            await asyncio.gather(*running, return_exceptions=True)

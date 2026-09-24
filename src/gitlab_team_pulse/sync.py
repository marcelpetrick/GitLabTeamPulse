"""Synchronization service: pulls GitLab data into SQLite with last-known-good semantics.

A failed step records a diagnostic and a failed sync state, but never replaces the previous
successful data. Every successful step commits its data, its sync state and a bumped
``data_version`` in one transaction.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import retention, store
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.gitlab.client import GitLabClient
from gitlab_team_pulse.gitlab.errors import GitLabCapabilityError, GitLabError
from gitlab_team_pulse.gitlab.models import ActivityEvent, Timelog, WorkItem
from gitlab_team_pulse.logging_setup import get_logger
from gitlab_team_pulse.models import SyncRun

log = get_logger("sync")

Clock = Callable[[], datetime]
RECENT_EVENTS = 12
ClientFactory = Callable[[Settings], GitLabClient]

NOT_CONFIGURED = (
    "GitLab is not configured: set TEAMPULSE_GITLAB_URL and a token "
    "(TEAMPULSE_GITLAB_TOKEN or a secret file)"
)
EPICS_UNAVAILABLE = (
    "Epics are not available from this GitLab (GraphQL work items with type EPIC); "
    "issues and merge requests are still shown"
)
EPICS_RECHECK = timedelta(hours=1)
INCOMPLETE_DIRECTORY = (
    "The token user is not an administrator: GitLab may hide blocked, deactivated or "
    "internal accounts, so the directory can be incomplete"
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def default_client_factory(settings: Settings) -> GitLabClient:
    token = settings.resolve_token()
    if not settings.gitlab_url or token is None:
        raise GitLabNotConfiguredError(NOT_CONFIGURED)
    return GitLabClient(
        settings.gitlab_url,
        token,
        timeout=settings.gitlab_timeout_seconds,
        max_retries=settings.gitlab_max_retries,
        concurrency=settings.gitlab_concurrency,
        verify=settings.tls_verify,
    )


class GitLabNotConfiguredError(GitLabError):
    kind = "configuration"


@dataclass
class RuntimeState:
    """In-memory crawler state exposed by ``/api/status``."""

    running: set[str] = field(default_factory=set)
    active_run_id: str | None = None
    active_trigger: str | None = None
    active_kind: str | None = None
    last_upstream_error: str | None = None
    last_upstream_ok_at: datetime | None = None
    epics: str = "unknown"  # unknown | available | unavailable
    epics_checked_at: datetime | None = None


class SyncService:
    """Owns all GitLab-to-SQLite synchronization. Methods are called by the scheduler."""

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        client_factory: ClientFactory = default_client_factory,
        clock: Clock = utcnow,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.client_factory = client_factory
        self.clock = clock
        self.runtime = RuntimeState()
        self._client: GitLabClient | None = None
        # Negative cache: projects whose metadata lookup failed (deleted, no access) are not
        # requested again on every run, only after the retention period.
        self._project_failures: dict[int, datetime] = {}

    # ------------------------------------------------------------------ plumbing

    def client(self) -> GitLabClient:
        if self._client is None:
            self._client = self.client_factory(self.settings)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def record_problem(self, subsystem: str, operation: str, message: str) -> None:
        """Persist a diagnostic on a best-effort basis (the database may be the problem)."""
        try:
            with self.session_factory() as session:
                store.record_error(
                    session,
                    subsystem=subsystem,
                    operation=operation,
                    message=message,
                    now=self.clock(),
                )
                store.bump_data_version(session)
                session.commit()
        except SQLAlchemyError:
            log.exception("could not persist diagnostic: %s", message)

    def _begin(self, kind: str, trigger: str) -> str:
        with self.session_factory() as session:
            run = store.start_run(session, kind=kind, trigger=trigger, now=self.clock())
            store.bump_data_version(session)
            session.commit()
        self.runtime.running.add(kind)
        self.runtime.active_run_id = run.id
        self.runtime.active_trigger = trigger
        self.runtime.active_kind = kind
        return run.id

    def _end(self, kind: str) -> None:
        self.runtime.running.discard(kind)
        if self.runtime.active_kind == kind:
            self.runtime.active_run_id = None
            self.runtime.active_trigger = None
            self.runtime.active_kind = None

    def _upstream(self, error: GitLabError | None) -> None:
        if error is None:
            self.runtime.last_upstream_error = None
            self.runtime.last_upstream_ok_at = self.clock()
        else:
            self.runtime.last_upstream_error = f"{error.kind}: {error}"

    # ------------------------------------------------------------------ directory

    async def sync_directory(self, trigger: str) -> str:
        """Refresh the full user directory. Returns the run status (ok/error)."""
        run_id = self._begin("directory", trigger)
        started = time.monotonic()
        try:
            try:
                client = self.client()
                me = await client.current_user()
                users = await client.list_users()
            except GitLabError as exc:
                self._upstream(exc)
                self._directory_failed(run_id, exc)
                log.error(
                    "directory refresh failed after %.1fs: %s", time.monotonic() - started, exc
                )
                return "error"
            self._upstream(None)
            now = self.clock()
            with self.session_factory() as session:
                added, updated = store.upsert_users(session, users, now)
                state = store.get_state(session, "directory")
                store.mark_attempt(state, now, run_id)
                store.mark_success(state, now)
                store.resolve_errors(
                    session, subsystem="directory", operation="list_users", now=now
                )
                if me.is_admin:
                    store.resolve_errors(
                        session, subsystem="directory", operation="completeness", now=now
                    )
                else:
                    store.record_error(
                        session,
                        subsystem="directory",
                        operation="completeness",
                        message=INCOMPLETE_DIRECTORY,
                        severity="warning",
                        now=now,
                    )
                summary = f"{len(users)} accounts ({added} new, {updated} updated)"
                run = session.get(SyncRun, run_id)
                if run is not None:
                    store.finish_run(run, status="ok", now=now, summary=summary)
                store.bump_data_version(session)
                store.bump_users_version(session)
                session.commit()
            log.info("directory refreshed: %s in %.1fs", summary, time.monotonic() - started)
            return "ok"
        finally:
            self._end("directory")

    def _directory_failed(self, run_id: str, exc: GitLabError) -> None:
        now = self.clock()
        with self.session_factory() as session:
            state = store.get_state(session, "directory")
            store.mark_attempt(state, now, run_id)
            store.mark_failure(state, str(exc))
            store.record_error(
                session,
                subsystem="directory",
                operation="list_users",
                message=str(exc),
                severity="warning" if exc.kind == "configuration" else "error",
                now=now,
            )
            run = session.get(SyncRun, run_id)
            if run is not None:
                store.finish_run(run, status="error", now=now, summary=str(exc))
            store.bump_data_version(session)
            store.bump_users_version(session)
            session.commit()

    def directory_healthy(self) -> bool:
        with self.session_factory() as session:
            state = store.find_state(session, "directory")
            return state is not None and state.status == "ok"

    def directory_due(self, now: datetime) -> bool:
        """Due hourly after a success; failed or never-run syncs retry sooner."""
        with self.session_factory() as session:
            state = store.find_state(session, "directory")
            if state is None or state.last_attempt_at is None:
                return True
            interval = self.settings.user_refresh_interval_seconds
            if state.status != "ok":
                interval = min(interval, self.settings.selected_refresh_interval_seconds)
            return now >= state.last_attempt_at + timedelta(seconds=interval)

    # ------------------------------------------------------------------ selected users

    def selected_due(self, now: datetime) -> bool:
        next_due = self.selected_next_due(now)
        return next_due is None or now >= next_due

    def selected_next_due(self, now: datetime) -> datetime | None:
        with self.session_factory() as session:
            state = store.find_state(session, "selected")
            if state is None or state.last_attempt_at is None:
                return None
            return state.last_attempt_at + timedelta(
                seconds=self.settings.selected_refresh_interval_seconds
            )

    def timelog_window_start(self, now: datetime) -> datetime:
        """Local midnight ``activity_days - 1`` days ago: the window spans seven calendar days."""
        local_today = now.astimezone(self.settings.tz).date()
        first_day = local_today - timedelta(days=self.settings.activity_days - 1)
        return datetime.combine(first_day, datetime.min.time(), self.settings.tz).astimezone(UTC)

    async def sync_selected(self, trigger: str, user_ids: list[int] | None = None) -> str:
        """Refresh work, activity and timelogs for selected users (or the given subset).

        Returns ok / partial / error. Categories fail independently; a failure never removes
        previously synchronized data.
        """
        run_id = self._begin("selected", trigger)
        started = time.monotonic()
        try:
            with self.session_factory() as session:
                users = [
                    (u.id, u.username)
                    for u in store.selected_users(session)
                    if user_ids is None or u.id in user_ids
                ]
            try:
                client = self.client() if users else None
            except GitLabError as exc:
                self._upstream(exc)
                return self._finish_selected(
                    run_id, user_ids, "error", str(exc), attempted=len(users), succeeded=0, exc=exc
                )
            outcomes: list[tuple[int, int, set[int]]] = []
            if client is not None:
                outcomes = await asyncio.gather(
                    *(self._sync_user(client, uid, username, run_id) for uid, username in users)
                )
                project_ids = set().union(*(ids for _, _, ids in outcomes))
                await self._resolve_projects(client, project_ids)
            ok_parts = sum(ok for ok, _, _ in outcomes)
            all_parts = sum(total for _, total, _ in outcomes)
            succeeded = sum(1 for ok, total, _ in outcomes if ok == total)
            if ok_parts == all_parts:
                status = "ok"
            elif ok_parts == 0:
                status = "error"
            else:
                status = "partial"
            summary = (
                f"{succeeded}/{len(users)} users fully refreshed "
                f"({ok_parts}/{all_parts} datasets) in {time.monotonic() - started:.1f}s"
            )
            log.info("selected refresh (%s): %s", trigger, summary)
            return self._finish_selected(
                run_id, user_ids, status, summary, attempted=len(users), succeeded=succeeded
            )
        finally:
            self._end("selected")

    def _finish_selected(
        self,
        run_id: str,
        user_ids: list[int] | None,
        status: str,
        summary: str,
        *,
        attempted: int,
        succeeded: int,
        exc: GitLabError | None = None,
    ) -> str:
        now = self.clock()
        with self.session_factory() as session:
            run = session.get(SyncRun, run_id)
            if run is not None:
                run.users_attempted = attempted
                run.users_succeeded = succeeded
                store.finish_run(run, status=status, now=now, summary=summary)
            if exc is not None:
                store.record_error(
                    session,
                    subsystem="sync",
                    operation="selected",
                    message=str(exc),
                    severity="warning" if exc.kind == "configuration" else "error",
                    now=now,
                )
            else:
                store.resolve_errors(session, subsystem="sync", operation="selected", now=now)
            if user_ids is None:
                state = store.get_state(session, "selected")
                if run is not None:
                    store.mark_attempt(state, run.started_at, run_id)
                if status == "error":
                    store.mark_failure(state, summary)
                else:
                    store.mark_success(state, now)
                    state.status = status
            store.bump_data_version(session)
            session.commit()
        return status

    async def _sync_user(
        self, client: GitLabClient, user_id: int, username: str, run_id: str
    ) -> tuple[int, int, set[int]]:
        """Returns (successful datasets, attempted datasets, referenced project IDs)."""
        results = await asyncio.gather(
            self._sync_work(client, user_id, username, run_id),
            self._sync_activity(client, user_id, run_id),
            self._sync_timelogs(client, user_id, username, run_id),
        )
        ok = sum(1 for success, _ in results if success)
        projects: set[int] = set().union(*(ids for _, ids in results))
        return ok, len(results), projects

    async def _category[T](
        self,
        category: str,
        user_id: int,
        run_id: str,
        fetch: Callable[[], Awaitable[T]],
        write: Callable[[Session, T, datetime], tuple[str | None, set[int]]],
    ) -> tuple[bool, set[int]]:
        """Fetch, then write atomically; on any failure keep the last known good data."""
        try:
            data = await fetch()
        except GitLabError as exc:
            self._upstream(exc)
            self._category_failed(category, user_id, run_id, str(exc), "sync")
            log.warning("user=%d %s refresh failed: %s", user_id, category, exc)
            return False, set()
        except Exception as exc:
            log.exception("user=%d %s refresh crashed", user_id, category)
            self._category_failed(category, user_id, run_id, f"internal error: {exc!r}", "sync")
            return False, set()
        self._upstream(None)
        now = self.clock()
        try:
            with self.session_factory() as session:
                cursor, project_ids = write(session, data, now)
                state = store.get_state(session, category, user_id)
                store.mark_attempt(state, now, run_id)
                store.mark_success(state, now, cursor=cursor)
                store.resolve_errors(
                    session, subsystem="sync", operation=category, user_id=user_id, now=now
                )
                store.bump_data_version(session)
                session.commit()
        except SQLAlchemyError as exc:
            log.exception("user=%d %s could not be stored", user_id, category)
            self._category_failed(
                category, user_id, run_id, f"database error: {type(exc).__name__}", "database"
            )
            return False, set()
        return True, project_ids

    def _category_failed(
        self, category: str, user_id: int, run_id: str, message: str, subsystem: str
    ) -> None:
        now = self.clock()
        with self.session_factory() as session:
            state = store.get_state(session, category, user_id)
            store.mark_attempt(state, now, run_id)
            store.mark_failure(state, message)
            store.record_error(
                session,
                subsystem=subsystem,
                operation=category,
                message=message,
                user_id=user_id,
                now=now,
            )
            store.bump_data_version(session)
            session.commit()

    async def _epics(
        self, client: GitLabClient, username: str, items: list[WorkItem], cutoff: datetime
    ) -> list[WorkItem]:
        """Best-effort epics for the top-level groups the user's work lives in."""
        now = self.clock()
        checked = self.runtime.epics_checked_at
        if self.runtime.epics == "unavailable" and checked and now - checked < EPICS_RECHECK:
            return []
        groups = {i.reference.split("/", 1)[0] for i in items if "/" in i.reference}
        if not groups:
            return []
        try:
            epics = await client.get_user_epics(username, groups, cutoff)
        except GitLabCapabilityError as exc:
            self.runtime.epics = "unavailable"
            self.runtime.epics_checked_at = now
            with self.session_factory() as session:
                store.record_error(
                    session,
                    subsystem="sync",
                    operation="epics",
                    message=f"{EPICS_UNAVAILABLE} ({exc})",
                    severity="warning",
                    now=now,
                )
                session.commit()
            log.info("epics unavailable: %s", exc)
            return []
        if self.runtime.epics != "available":
            with self.session_factory() as session:
                store.resolve_errors(session, subsystem="sync", operation="epics", now=now)
                session.commit()
        self.runtime.epics = "available"
        self.runtime.epics_checked_at = now
        return epics

    async def _sync_work(
        self, client: GitLabClient, user_id: int, username: str, run_id: str
    ) -> tuple[bool, set[int]]:
        cutoff = self.clock() - timedelta(days=self.settings.work_window_days)

        async def fetch() -> list[WorkItem]:
            items = await client.get_user_work(user_id, cutoff)
            return [*items, *await self._epics(client, username, items, cutoff)]

        def write(session: Session, items: list[WorkItem], now: datetime) -> tuple[None, set[int]]:
            store.replace_user_work(session, user_id, items, now)
            return None, {i.project_id for i in items if i.project_id is not None}

        return await self._category("work", user_id, run_id, fetch, write)

    async def _sync_activity(
        self, client: GitLabClient, user_id: int, run_id: str
    ) -> tuple[bool, set[int]]:
        now = self.clock()
        with self.session_factory() as session:
            state = store.find_state(session, "activity", user_id)
            cursor = state.cursor if state and state.status != "never" else None
        after = now.date() - timedelta(days=self.settings.activity_days + 1)
        if cursor:
            # Incremental: only days since the newest stored event (GitLab's `after` is by date).
            after = max(after, datetime.fromisoformat(cursor).date() - timedelta(days=1))

        async def fetch() -> list[ActivityEvent]:
            events = await client.get_user_recent_activity(user_id, after)
            if cursor is None and len(events) < RECENT_EVENTS:
                older = await client.get_user_recent_activity(user_id, limit=RECENT_EVENTS)
                events = [*events, *(e for e in older if e.id not in {x.id for x in events})]
            return events

        def write(
            session: Session, events: list[ActivityEvent], stamp: datetime
        ) -> tuple[str | None, set[int]]:
            store.add_user_events(session, user_id, events, stamp)
            newest = max((e.created_at for e in events), default=None)
            previous = datetime.fromisoformat(cursor) if cursor else None
            candidates = [d for d in (newest, previous) if d is not None]
            new_cursor = max(candidates).isoformat() if candidates else None
            return new_cursor, {e.project_id for e in events if e.project_id is not None}

        return await self._category("activity", user_id, run_id, fetch, write)

    async def _sync_timelogs(
        self, client: GitLabClient, user_id: int, username: str, run_id: str
    ) -> tuple[bool, set[int]]:
        now = self.clock()
        start = self.timelog_window_start(now)

        def write(session: Session, logs: list[Timelog], stamp: datetime) -> tuple[None, set[int]]:
            store.replace_user_timelogs(session, user_id, logs, start, stamp)
            return None, {t.project_id for t in logs if t.project_id is not None}

        return await self._category(
            "timelogs",
            user_id,
            run_id,
            lambda: client.get_user_timelogs(username, start, now),
            write,
        )

    async def _resolve_projects(self, client: GitLabClient, project_ids: set[int]) -> None:
        """Fetch metadata only for referenced projects that are unknown or older than a day."""
        now = self.clock()
        with self.session_factory() as session:
            missing = store.projects_needing_refresh(
                session, project_ids, now - timedelta(hours=self.settings.retention_hours)
            )
        retry_after = timedelta(hours=self.settings.retention_hours)
        missing = {
            pid
            for pid in missing
            if pid not in self._project_failures or now - self._project_failures[pid] >= retry_after
        }
        if not missing:
            return
        results = await asyncio.gather(
            *(client.get_project(pid) for pid in sorted(missing)), return_exceptions=True
        )
        with self.session_factory() as session:
            for pid, result in zip(sorted(missing), results, strict=True):
                if isinstance(result, BaseException):
                    log.warning("project=%d metadata unavailable: %s", pid, result)
                    self._project_failures[pid] = now
                    continue
                self._project_failures.pop(pid, None)
                store.upsert_project(session, result, now)
            store.bump_data_version(session)
            session.commit()

    # ------------------------------------------------------------------ cleanup

    def cleanup(self) -> str:
        """Apply retention; failures are recorded, never raised into the scheduler."""
        now = self.clock()
        try:
            with self.session_factory() as session:
                stats = retention.cleanup(session, self.settings, now)
                store.resolve_errors(session, subsystem="cleanup", now=now)
                session.commit()
                bind = session.get_bind()
            if isinstance(bind, Engine):
                retention.vacuum_if_needed(bind)
        except SQLAlchemyError as exc:
            log.exception("cleanup failed")
            with self.session_factory() as session:
                store.record_error(
                    session,
                    subsystem="cleanup",
                    operation="retention",
                    message=f"cleanup failed: {type(exc).__name__}",
                    now=now,
                )
                session.commit()
            return "error"
        log.info("cleanup: %s", stats.summary())
        return "ok"

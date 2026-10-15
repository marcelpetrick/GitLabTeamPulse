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
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import retention, store
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.freshness import FAILED_STATUSES
from gitlab_team_pulse.gitlab.client import GitLabClient
from gitlab_team_pulse.gitlab.errors import (
    GitLabCapabilityError,
    GitLabError,
    GitLabForbiddenError,
    GitLabNotFoundError,
)
from gitlab_team_pulse.gitlab.models import (
    ActivityEvent,
    Timelog,
    WorkItem,
    counts_as_contribution,
    history_metric,
)
from gitlab_team_pulse.logging_setup import get_logger
from gitlab_team_pulse.models import Milestone, SyncRun, User

log = get_logger("sync")

Clock = Callable[[], datetime]
RECENT_EVENTS = 12
CONTRIBUTION_WEEKS = 53
# Bumped when the event-derived history gains data: older cursors then refetch the year once.
HISTORY_REVISION = "h1"
# Late or edited time entries are picked up by re-reading this many days on every refresh.
TIMELOG_RESWEEP_DAYS = 31
HISTORY_CATEGORIES = store.HISTORY_CATEGORIES
TOO_MANY_TRACKED = (
    "More team members than TEAMPULSE_MAX_TRACKED_USERS: the history of {count} team "
    "members is not refreshed"
)


def contribution_grid_start(today: date) -> date:
    """First day of the 53-week calendar grid: a Sunday, as on GitLab profiles."""
    start = today - timedelta(weeks=CONTRIBUTION_WEEKS - 1)
    return start - timedelta(days=(start.weekday() + 1) % 7)


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
    selected_scope: frozenset[int] | None = None  # users of the running selected run; None = all
    epics: str = "unknown"  # unknown | available | unavailable
    epics_checked_at: datetime | None = None
    epics_failing_since: datetime | None = None


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
        self.runtime.selected_scope = frozenset(user_ids) if user_ids is not None else None
        started = time.monotonic()
        try:
            users, team_only = self.tracked_users(user_ids)
            try:
                client = self.client() if users or team_only else None
            except GitLabError as exc:
                self._upstream(exc)
                return self._finish_selected(
                    run_id, user_ids, "error", str(exc), attempted=len(users), succeeded=0, exc=exc
                )
            outcomes: list[tuple[int, int, set[int]]] = []
            if client is not None:
                outcomes = await asyncio.gather(
                    *(self._sync_user(client, uid, username, run_id) for uid, username in users),
                    *(
                        self._sync_user(client, uid, username, run_id, full=False)
                        for uid, username in team_only
                    ),
                )
                project_ids = set().union(*(ids for _, _, ids in outcomes))
                await self._resolve_projects(client, project_ids)
                if self.milestones_due(self.clock()):
                    await self.sync_milestones(client, run_id)
            outcomes = [o for o in outcomes if o[1]]
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
                f"{succeeded}/{len(outcomes)} users fully refreshed "
                f"({ok_parts}/{all_parts} datasets) in {time.monotonic() - started:.1f}s"
            )
            log.info("selected refresh (%s): %s", trigger, summary)
            return self._finish_selected(
                run_id, user_ids, status, summary, attempted=len(outcomes), succeeded=succeeded
            )
        finally:
            self.runtime.selected_scope = None
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

    def tracked_users(
        self, user_ids: list[int] | None = None
    ) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        """(selected users, team members who are not selected), optionally limited to IDs.

        Team members beyond ``max_tracked_users`` (counting the selection) are left out and
        reported as a diagnostic; the dashboard selection itself is never capped.
        """
        with self.session_factory() as session:
            selected = store.selected_users(session)
            selected_ids = {u.id for u in selected}
            members = store.tracked_team_user_ids(session) - selected_ids
            extra = list(
                session.scalars(select(User).where(User.id.in_(members)).order_by(User.id))
            )
            room = max(self.settings.max_tracked_users - len(selected), 0)
            dropped = len(extra) - room
            if dropped > 0:
                store.record_error(
                    session,
                    subsystem="sync",
                    operation="tracked_users",
                    message=TOO_MANY_TRACKED.format(count=dropped),
                    severity="warning",
                    now=self.clock(),
                )
            else:
                store.resolve_errors(
                    session, subsystem="sync", operation="tracked_users", now=self.clock()
                )
            session.commit()
        scope = None if user_ids is None else set(user_ids)
        return (
            [(u.id, u.username) for u in selected if scope is None or u.id in scope],
            [(u.id, u.username) for u in extra[:room] if scope is None or u.id in scope],
        )

    async def _sync_user(
        self, client: GitLabClient, user_id: int, username: str, run_id: str, *, full: bool = True
    ) -> tuple[int, int, set[int]]:
        """Returns (successful datasets, attempted datasets, referenced project IDs).

        ``full`` syncs the dashboard datasets; the hourly history categories run when due, also
        for team members who are not on the dashboard (``full=False``).
        """
        now = self.clock()
        jobs = []
        if full:
            jobs += [
                self._sync_work(client, user_id, username, run_id),
                self._sync_activity(client, user_id, run_id),
                self._sync_timelogs(client, user_id, username, run_id),
            ]
        if self.history_due("contributions", user_id, now):
            jobs.append(self._sync_contributions(client, user_id, run_id))
        if self.history_due("timelog_history", user_id, now):
            jobs.append(self._sync_timelog_history(client, user_id, username, run_id))
        if self.history_due("outcomes", user_id, now):
            jobs.append(self._sync_outcomes(client, user_id, run_id))
        results = await asyncio.gather(*jobs)
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
    ) -> list[WorkItem] | None:
        """Best-effort epics for the top-level groups the user's work lives in.

        Returns None when epics worked before but fail now: the caller then keeps the stored
        epics while issues and merge requests still refresh.
        """
        now = self.clock()
        checked = self.runtime.epics_checked_at
        if self.runtime.epics == "unavailable" and checked and now - checked < EPICS_RECHECK:
            return []
        groups = {i.reference.split("/", 1)[0] for i in items if "/" in i.reference}
        if not groups:
            return None  # no group to ask this run: keep the stored epics (last known good)
        try:
            # All assigned epics, then keep the open ones however old (stalled work must not
            # drop out of view) and the others updated inside the work window.
            fetched = await client.get_user_epics(username, groups, None)
            epics = [e for e in fetched if e.state == "opened" or e.updated_at >= cutoff]
        except GitLabCapabilityError as exc:
            if self.runtime.epics == "available":
                since = self.runtime.epics_failing_since or now
                self.runtime.epics_failing_since = since
                if now - since < EPICS_RECHECK:
                    # Possibly temporary: keep the last known good epics, refresh the rest.
                    log.warning("epics failed, keeping stored epics: %s", exc)
                    return None
                # Failing for longer than the recheck window: the capability is really gone.
            self.runtime.epics = "unavailable"
            self.runtime.epics_failing_since = None
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
        self.runtime.epics_failing_since = None
        return epics

    async def _sync_work(
        self, client: GitLabClient, user_id: int, username: str, run_id: str
    ) -> tuple[bool, set[int]]:
        cutoff = self.clock() - timedelta(days=self.settings.work_window_days)

        async def fetch() -> tuple[list[WorkItem], bool]:
            items = await client.get_user_work(user_id, cutoff)
            epics = await self._epics(client, username, items, cutoff)
            return [*items, *(epics or [])], epics is None

        def write(
            session: Session, fetched: tuple[list[WorkItem], bool], now: datetime
        ) -> tuple[None, set[int]]:
            items, keep_epics = fetched
            keep = frozenset({"epic"}) if keep_epics else frozenset()
            store.replace_user_work(session, user_id, items, now, keep_kinds=keep)
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

    def contribution_window_start(self, now: datetime) -> date:
        """First day of the calendar grid in the configured time zone."""
        return contribution_grid_start(now.astimezone(self.settings.tz).date())

    def history_due(self, category: str, user_id: int | None, now: datetime) -> bool:
        """History changes slowly: refresh at most every N minutes, not every run.

        A failed attempt is retried on the next run, so a failed backfill does not leave the
        history empty for a whole interval.
        """
        with self.session_factory() as session:
            state = store.find_state(session, category, user_id)
            if state is None or state.last_attempt_at is None or state.status in FAILED_STATUSES:
                return True
            interval = timedelta(minutes=self.settings.contributions_refresh_minutes)
            return now >= state.last_attempt_at + interval

    def contributions_due(self, user_id: int, now: datetime) -> bool:
        return self.history_due("contributions", user_id, now)

    def milestones_due(self, now: datetime) -> bool:
        return self.settings.milestones_max > 0 and self.history_due("milestones", None, now)

    def local_midnight(self, day: date) -> datetime:
        return datetime.combine(day, datetime.min.time(), self.settings.tz).astimezone(UTC)

    async def _sync_contributions(
        self, client: GitLabClient, user_id: int, run_id: str
    ) -> tuple[bool, set[int]]:
        """Daily contribution counts, computed from events with GitLab's calendar rule.

        The first sync backfills the whole grid; later syncs recount only from the last
        counted day (the previous day is included to absorb late-arriving events). The cursor
        records the time zone the days were bucketed in; a changed zone forces a full backfill.
        """
        now = self.clock()
        start = self.contribution_window_start(now)
        zone = self.settings.timezone
        with self.session_factory() as session:
            state = store.find_state(session, "contributions", user_id)
            cursor = state.cursor if state and state.last_success_at else None
        from_day = start
        if cursor is not None:
            last_day, _, rest = cursor.partition("@")
            cursor_zone, _, revision = rest.partition("@")
            # Cursors from before the history revision lack per-project history: refetch once.
            if cursor_zone == zone and revision == HISTORY_REVISION:
                from_day = max(start, date.fromisoformat(last_day) - timedelta(days=1))
        tz = self.settings.tz

        async def fetch() -> list[ActivityEvent]:
            # GitLab's `after` is an exclusive UTC date: step back two days to cover time zones.
            # A truncated backfill must fail rather than store the missing oldest days as zero.
            return await client.get_user_recent_activity(
                user_id, from_day - timedelta(days=2), complete=True
            )

        def write(
            session: Session, events: list[ActivityEvent], stamp: datetime
        ) -> tuple[str, set[int]]:
            counts: dict[date, int] = {}
            history: dict[tuple[date, int, str], int] = {}
            for event in events:
                day = event.created_at.astimezone(tz).date()
                if day < from_day:
                    continue
                if counts_as_contribution(event):
                    counts[day] = counts.get(day, 0) + 1
                metric = history_metric(event)
                if metric is not None:
                    key = (day, event.project_id or 0, metric)
                    history[key] = history.get(key, 0) + 1
            store.replace_contribution_days(session, user_id, counts, from_day)
            store.replace_activity_daily(session, user_id, history, from_day)
            cursor = f"{stamp.astimezone(tz).date().isoformat()}@{zone}@{HISTORY_REVISION}"
            return cursor, {pid for _, pid, _ in history if pid}

        return await self._category("contributions", user_id, run_id, fetch, write)

    async def _sync_timelog_history(
        self, client: GitLabClient, user_id: int, username: str, run_id: str
    ) -> tuple[bool, set[int]]:
        """Logged seconds per local day and project for the history window.

        The first sync backfills the window; later syncs re-read the last weeks because time is
        often logged late or edited. A changed time zone forces a full backfill.
        """
        now = self.clock()
        tz = self.settings.tz
        zone = self.settings.timezone
        window = self.contribution_window_start(now)
        with self.session_factory() as session:
            state = store.find_state(session, "timelog_history", user_id)
            cursor = state.cursor if state and state.last_success_at else None
        from_day = window
        if cursor is not None and cursor.partition("@")[2] == zone:
            today = now.astimezone(tz).date()
            from_day = max(window, today - timedelta(days=TIMELOG_RESWEEP_DAYS))
        start = self.local_midnight(from_day)

        def write(session: Session, logs: list[Timelog], stamp: datetime) -> tuple[str, set[int]]:
            seconds: dict[tuple[date, int], tuple[str | None, int]] = {}
            for entry in logs:
                key = (entry.spent_at.astimezone(tz).date(), entry.project_id or 0)
                path, total = seconds.get(key, (entry.project_path, 0))
                seconds[key] = (path or entry.project_path, total + entry.seconds)
            store.replace_timelog_daily(session, user_id, seconds, from_day)
            stamp_day = stamp.astimezone(tz).date().isoformat()
            return f"{stamp_day}@{zone}", {pid for _, pid in seconds if pid}

        return await self._category(
            "timelog_history",
            user_id,
            run_id,
            lambda: client.get_user_timelogs(username, start, now),
            write,
        )

    async def _sync_outcomes(
        self, client: GitLabClient, user_id: int, run_id: str
    ) -> tuple[bool, set[int]]:
        """Merged MRs (author) and closed issues (assignee), incrementally by update time."""
        now = self.clock()
        with self.session_factory() as session:
            state = store.find_state(session, "outcomes", user_id)
            cursor = state.cursor if state and state.last_success_at else None
        window = self.local_midnight(self.contribution_window_start(now))
        updated_after = window
        if cursor is not None:
            updated_after = max(window, datetime.fromisoformat(cursor) - timedelta(days=1))

        def write(session: Session, items: list[WorkItem], stamp: datetime) -> tuple[str, set[int]]:
            store.apply_outcomes(session, user_id, items)
            return now.isoformat(), {i.project_id for i in items if i.project_id is not None}

        return await self._category(
            "outcomes",
            user_id,
            run_id,
            lambda: client.get_user_outcomes(user_id, updated_after),
            write,
        )

    def milestones_to_refresh(self) -> list[Milestone]:
        """The milestones the dashboard shows (same rule, same cap)."""
        with self.session_factory() as session:
            selected = [u.id for u in store.selected_users(session)]
            return store.relevant_milestones(session, selected, self.settings.milestones_max)

    async def sync_milestones(self, client: GitLabClient, run_id: str) -> bool:
        """Progress of the relevant milestones, computed from their issues (no Premium API).

        A milestone that cannot be read keeps its last statistics; the others still update.
        """
        milestones = self.milestones_to_refresh()
        results = await asyncio.gather(
            *(
                client.get_milestone_issues(m.id, project_id=m.project_id, group_id=m.group_id)
                for m in milestones
            ),
            return_exceptions=True,
        )
        now = self.clock()
        today = now.astimezone(self.settings.tz).date()
        failures: list[str] = []
        with self.session_factory() as session:
            for milestone, result in zip(milestones, results, strict=True):
                if isinstance(result, GitLabError):
                    failures.append(f"{milestone.title}: {result}")
                    continue
                if isinstance(result, BaseException):
                    failures.append(f"{milestone.title}: internal error {result!r}")
                    continue
                store.set_milestone_stats(
                    session, milestone.id, store.milestone_stats(result, today), now
                )
            state = store.get_state(session, "milestones")
            store.mark_attempt(state, now, run_id)
            # Per-milestone problems are diagnostics; the category itself keeps its hourly
            # cadence, so one unreadable milestone does not refetch all of them every run.
            store.mark_success(state, now)
            if failures:
                message = "; ".join(failures)
                state.last_error = message[: store.MAX_MESSAGE]
                store.record_error(
                    session,
                    subsystem="sync",
                    operation="milestones",
                    message=f"Milestone progress could not be read: {message}",
                    severity="warning",
                    now=now,
                )
            else:
                store.resolve_errors(session, subsystem="sync", operation="milestones", now=now)
            store.bump_data_version(session)
            session.commit()
        if failures:
            log.warning("milestones: %s", "; ".join(failures))
        return not failures

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
                    if isinstance(result, GitLabNotFoundError | GitLabForbiddenError):
                        # Deleted or inaccessible: do not ask again before the retention period.
                        # Transient failures (timeouts, 5xx) are retried on the next run.
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

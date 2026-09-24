"""Synchronization service: pulls GitLab data into SQLite with last-known-good semantics.

A failed step records a diagnostic and a failed sync state, but never replaces the previous
successful data. Every successful step commits its data, its sync state and a bumped
``data_version`` in one transaction.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import store
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.gitlab.client import GitLabClient
from gitlab_team_pulse.gitlab.errors import GitLabError
from gitlab_team_pulse.logging_setup import get_logger
from gitlab_team_pulse.models import SyncRun

log = get_logger("sync")

Clock = Callable[[], datetime]
ClientFactory = Callable[[Settings], GitLabClient]

NOT_CONFIGURED = (
    "GitLab is not configured: set TEAMPULSE_GITLAB_URL and a token "
    "(TEAMPULSE_GITLAB_TOKEN or a secret file)"
)
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


class SyncService:
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

    # ------------------------------------------------------------------ plumbing

    def client(self) -> GitLabClient:
        if self._client is None:
            self._client = self.client_factory(self.settings)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

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
            session.commit()

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

"""Persistence helpers operating on a caller-owned SQLAlchemy session.

Callers own the transaction: every function here only stages changes, so a sync step can
write its data, its sync state and the bumped ``data_version`` in one atomic commit.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from gitlab_team_pulse.models import AppState, ErrorRecord, SyncRun, SyncState

DATA_VERSION = "data_version"
MAX_MESSAGE = 2000


def get_value(session: Session, key: str) -> str | None:
    row = session.get(AppState, key)
    return row.value if row else None


def set_value(session: Session, key: str, value: str) -> None:
    row = session.get(AppState, key)
    if row is None:
        session.add(AppState(key=key, value=value))
    else:
        row.value = value


def data_version(session: Session) -> int:
    return int(get_value(session, DATA_VERSION) or 0)


def bump_data_version(session: Session) -> int:
    """Increment the change counter the browser polls to detect new cached data."""
    version = data_version(session) + 1
    set_value(session, DATA_VERSION, str(version))
    return version


# ---------------------------------------------------------------------- sync state


def state_key(category: str, user_id: int | None = None) -> str:
    return category if user_id is None else f"{category}:{user_id}"


def get_state(session: Session, category: str, user_id: int | None = None) -> SyncState:
    key = state_key(category, user_id)
    state = session.get(SyncState, key)
    if state is None:
        state = SyncState(key=key, category=category, user_id=user_id, status="never")
        session.add(state)
    return state


def find_state(session: Session, category: str, user_id: int | None = None) -> SyncState | None:
    return session.get(SyncState, state_key(category, user_id))


def mark_attempt(state: SyncState, now: datetime, run_id: str | None) -> None:
    state.last_attempt_at = now
    state.run_id = run_id


def mark_success(
    state: SyncState,
    now: datetime,
    *,
    cursor: str | None = None,
    next_due_at: datetime | None = None,
) -> None:
    state.status = "ok"
    state.last_success_at = now
    state.last_error = None
    if cursor is not None:
        state.cursor = cursor
    state.next_due_at = next_due_at


def mark_failure(
    state: SyncState, message: str, *, status: str = "error", next_due_at: datetime | None = None
) -> None:
    """Record a failed attempt; ``last_success_at`` (the last known good) stays untouched."""
    state.status = status
    state.last_error = message[:MAX_MESSAGE]
    state.next_due_at = next_due_at


# ---------------------------------------------------------------------- errors


def record_error(
    session: Session,
    *,
    subsystem: str,
    operation: str,
    message: str,
    now: datetime,
    severity: str = "error",
    user_id: int | None = None,
    project_id: int | None = None,
    details: str | None = None,
) -> ErrorRecord:
    """Persist a problem, folding repeats of the same open problem into one row."""
    message = message[:MAX_MESSAGE]
    existing = session.scalars(
        select(ErrorRecord).where(
            ErrorRecord.resolved_at.is_(None),
            ErrorRecord.subsystem == subsystem,
            ErrorRecord.operation == operation,
            ErrorRecord.user_id.is_(None) if user_id is None else ErrorRecord.user_id == user_id,
            ErrorRecord.message == message,
        )
    ).first()
    if existing is not None:
        existing.occurred_at = now
        existing.occurrences += 1
        existing.severity = severity
        existing.details = details
        return existing
    record = ErrorRecord(
        first_occurred_at=now,
        occurred_at=now,
        occurrences=1,
        severity=severity,
        subsystem=subsystem,
        operation=operation,
        user_id=user_id,
        project_id=project_id,
        message=message,
        details=details,
    )
    session.add(record)
    return record


def resolve_errors(
    session: Session,
    *,
    subsystem: str,
    now: datetime,
    operation: str | None = None,
    user_id: int | None = None,
) -> int:
    """Mark open problems as resolved after the matching operation succeeded again."""
    statement = update(ErrorRecord).where(
        ErrorRecord.resolved_at.is_(None), ErrorRecord.subsystem == subsystem
    )
    if operation is not None:
        statement = statement.where(ErrorRecord.operation == operation)
    if user_id is not None:
        statement = statement.where(ErrorRecord.user_id == user_id)
    result = session.execute(statement.values(resolved_at=now))
    return int(result.rowcount)  # type: ignore[attr-defined]


def unresolved_error_count(session: Session) -> int:
    count = session.scalar(
        select(func.count()).select_from(ErrorRecord).where(ErrorRecord.resolved_at.is_(None))
    )
    return int(count or 0)


def list_errors(
    session: Session, *, limit: int = 100, include_resolved: bool = True
) -> list[ErrorRecord]:
    statement = select(ErrorRecord).order_by(ErrorRecord.occurred_at.desc(), ErrorRecord.id.desc())
    if not include_resolved:
        statement = statement.where(ErrorRecord.resolved_at.is_(None))
    return list(session.scalars(statement.limit(limit)))


# ---------------------------------------------------------------------- runs


def start_run(session: Session, *, kind: str, trigger: str, now: datetime) -> SyncRun:
    run = SyncRun(
        id=str(uuid.uuid4()),
        kind=kind,
        trigger=trigger,
        started_at=now,
        status="running",
        users_attempted=0,
        users_succeeded=0,
        summary="",
    )
    session.add(run)
    return run


def finish_run(run: SyncRun, *, status: str, now: datetime, summary: str) -> None:
    run.status = status
    run.finished_at = now
    run.summary = summary[:MAX_MESSAGE]


def latest_run(session: Session, kind: str) -> SyncRun | None:
    return session.scalars(
        select(SyncRun).where(SyncRun.kind == kind).order_by(SyncRun.started_at.desc()).limit(1)
    ).first()

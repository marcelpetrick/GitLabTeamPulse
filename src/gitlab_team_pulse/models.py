"""SQLAlchemy ORM models: the local cache and application state.

GitLab objects with a stable instance-wide ID (users, projects, events, timelogs) use that ID as
the primary key. Timestamps are stored as naive UTC in SQLite and returned timezone-aware.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC timestamps naively; always hand back aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not accepted; use timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map: dict[Any, Any] = {datetime: UTCDateTime()}  # noqa: RUF012


class User(Base):
    """A GitLab account from the directory; ``id`` is the GitLab user ID."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    avatar_url: Mapped[str | None] = mapped_column(Text)
    web_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None]
    state: Mapped[str] = mapped_column(String(32), default="unknown")
    account_type: Mapped[str] = mapped_column(String(32), default="unknown")
    selected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    selected_at: Mapped[datetime | None]
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]


class AppState(Base):
    """Small key/value store, e.g. the monotonically increasing ``data_version``."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class SyncState(Base):
    """Per-category (and per-user) synchronization bookkeeping, e.g. ``activity:42``."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="never")
    last_attempt_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    next_due_at: Mapped[datetime | None]
    cursor: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(36))


class SyncRun(Base):
    """One scheduled, manual or startup synchronization run."""

    __tablename__ = "sync_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    trigger: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(index=True)
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(16), default="running")
    users_attempted: Mapped[int] = mapped_column(Integer, default=0)
    users_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")


class ErrorRecord(Base):
    """A persisted backend/crawler problem shown in the diagnostics drawer.

    Repeated identical unresolved problems are folded into one row (``occurrences``) so an
    outage cannot grow the table without bound.
    """

    __tablename__ = "errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    first_occurred_at: Mapped[datetime]
    occurred_at: Mapped[datetime] = mapped_column(index=True)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    severity: Mapped[str] = mapped_column(String(16), default="error")
    subsystem: Mapped[str] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    project_id: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(index=True)

    __table_args__ = (Index("ix_errors_open_key", "subsystem", "operation", "user_id"),)


class Project(Base):
    """Project metadata, resolved on demand only for projects referenced by cached data."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255))
    path_with_namespace: Mapped[str] = mapped_column(String(512))
    web_url: Mapped[str | None] = mapped_column(Text)
    refreshed_at: Mapped[datetime] = mapped_column(index=True)


class WorkItemRecord(Base):
    """An issue, merge request or other assignable item, in any state."""

    __tablename__ = "work_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    gitlab_id: Mapped[int] = mapped_column(Integer)
    iid: Mapped[int] = mapped_column(Integer)
    project_id: Mapped[int | None] = mapped_column(Integer, index=True)
    reference: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), index=True)
    issue_type: Mapped[str | None] = mapped_column(String(32))
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    milestone: Mapped[str | None] = mapped_column(String(255))
    priority: Mapped[str | None] = mapped_column(String(64))
    due_date: Mapped[date | None]
    created_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(index=True)
    closed_at: Mapped[datetime | None]
    web_url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(255))
    assignees: Mapped[list[str]] = mapped_column(JSON, default=list)
    draft: Mapped[bool] = mapped_column(Boolean, default=False)
    refreshed_at: Mapped[datetime]

    __table_args__ = (UniqueConstraint("kind", "gitlab_id", name="uq_work_items_kind_gitlab_id"),)


class WorkItemAssignee(Base):
    """Links a work item to a selected user; supports multiple assignees and reviewers."""

    __tablename__ = "work_item_assignees"

    work_item_id: Mapped[int] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    relation: Mapped[str] = mapped_column(String(16), primary_key=True)
    observed_at: Mapped[datetime]


class ActivityEventRecord(Base):
    """A GitLab user event; ``id`` is the GitLab event ID."""

    __tablename__ = "activity_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project_id: Mapped[int | None] = mapped_column(Integer, index=True)
    category: Mapped[str] = mapped_column(String(16))
    action_name: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime]
    url_path: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime]

    __table_args__ = (Index("ix_activity_events_user_time", "user_id", "occurred_at"),)


class TimelogRecord(Base):
    """An actual time entry logged in GitLab; never estimated."""

    __tablename__ = "timelogs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project_id: Mapped[int | None] = mapped_column(Integer, index=True)
    project_path: Mapped[str | None] = mapped_column(String(512))
    spent_at: Mapped[datetime]
    seconds: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(Text)
    target_kind: Mapped[str | None] = mapped_column(String(16))
    target_iid: Mapped[int | None] = mapped_column(Integer)
    target_title: Mapped[str | None] = mapped_column(Text)
    web_url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime]

    __table_args__ = (Index("ix_timelogs_user_time", "user_id", "spent_at"),)

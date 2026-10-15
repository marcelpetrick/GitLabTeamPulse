"""Rolling retention that keeps SQLite compact without ever blanking the dashboard.

Rules (VISION section 18):

* A selected user's facts are trimmed to the visible window **only after** a recent successful
  sync of that category. If GitLab has been unreachable longer than the retention period, the
  last known good data is kept regardless of age.
* The twelve newest events per user are always kept, even when older than the chart window.
* Deselected users keep their short-lived cache until it is older than the retention period.
* Team members who are not selected keep their history (daily aggregates and outcomes) while
  their short-lived dashboard cache expires; leaving every team purges the history as well.
* History aggregates never outlive the 53-week calendar window.
* Selection state is never touched; resolved diagnostics and old runs expire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine, delete, exists, func, or_, select, text
from sqlalchemy.orm import Session

from gitlab_team_pulse import store
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.models import (
    ActivityDaily,
    ActivityEventRecord,
    ContributionDay,
    ErrorRecord,
    Milestone,
    Outcome,
    Project,
    SyncRun,
    SyncState,
    TimelogDaily,
    TimelogRecord,
    User,
    WorkItemAssignee,
    WorkItemRecord,
)

RECENT_EVENTS_KEPT = 12
VACUUM_FREE_PAGES = 256
DASHBOARD_CATEGORIES = ("work", "activity", "timelogs")
USER_CATEGORIES = (*DASHBOARD_CATEGORIES, *store.HISTORY_CATEGORIES)
CONTRIBUTION_DAYS_KEPT = 7 * 54  # the 53-week grid plus a week of slack
HISTORY_TABLES = (
    (ContributionDay, "contribution_days"),
    (ActivityDaily, "history_days"),
    (TimelogDaily, "history_days"),
    (Outcome, "outcomes"),
)


@dataclass
class CleanupStats:
    events: int = 0
    timelogs: int = 0
    contribution_days: int = 0
    history_days: int = 0
    outcomes: int = 0
    milestones: int = 0
    work_links: int = 0
    work_items: int = 0
    projects: int = 0
    runs: int = 0
    errors: int = 0
    states: int = 0
    kept_last_known_good: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{name}={value}"
            for name, value in vars(self).items()
            if isinstance(value, int) and value
        ]
        if self.kept_last_known_good:
            parts.append(f"kept_last_known_good={len(self.kept_last_known_good)}")
        return ", ".join(parts) or "nothing to remove"


def window_start(settings: Settings, now: datetime) -> datetime:
    """Oldest instant still needed for the seven-day views, plus one day of sync overlap."""
    return now - timedelta(days=settings.activity_days + 1)


def _recent(state: SyncState | None, cutoff: datetime) -> bool:
    return bool(state and state.last_success_at and state.last_success_at >= cutoff)


def _trim_events(session: Session, user_id: int, before: datetime) -> int:
    keep = (
        select(ActivityEventRecord.id)
        .where(ActivityEventRecord.user_id == user_id)
        .order_by(ActivityEventRecord.occurred_at.desc(), ActivityEventRecord.id.desc())
        .limit(RECENT_EVENTS_KEPT)
    )
    result = session.execute(
        delete(ActivityEventRecord).where(
            ActivityEventRecord.user_id == user_id,
            ActivityEventRecord.occurred_at < before,
            ActivityEventRecord.id.not_in(keep),
        )
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


def _count(stats: CleanupStats, attr: str, result: object) -> None:
    setattr(stats, attr, getattr(stats, attr) + int(result.rowcount))  # type: ignore[attr-defined]


def _purge_dashboard_cache(session: Session, user_id: int, stats: CleanupStats) -> None:
    """Drop the short-lived dashboard facts and their sync states of one user."""
    for model, attr in ((ActivityEventRecord, "events"), (TimelogRecord, "timelogs")):
        _count(stats, attr, session.execute(delete(model).where(model.user_id == user_id)))
    _count(
        stats,
        "work_links",
        session.execute(delete(WorkItemAssignee).where(WorkItemAssignee.user_id == user_id)),
    )
    _count(
        stats,
        "states",
        session.execute(
            delete(SyncState).where(
                SyncState.user_id == user_id, SyncState.category.in_(DASHBOARD_CATEGORIES)
            )
        ),
    )


def _purge_user(session: Session, user_id: int, stats: CleanupStats) -> None:
    _purge_dashboard_cache(session, user_id, stats)
    for model, attr in HISTORY_TABLES:
        _count(stats, attr, session.execute(delete(model).where(model.user_id == user_id)))
    _count(stats, "states", session.execute(delete(SyncState).where(SyncState.user_id == user_id)))


def cleanup(session: Session, settings: Settings, now: datetime) -> CleanupStats:
    """Apply the retention rules inside the caller's transaction."""
    stats = CleanupStats()
    cutoff = now - timedelta(hours=settings.retention_hours)
    visible_from = window_start(settings, now)
    states = {state.key: state for state in session.scalars(select(SyncState))}

    user_ids_with_data = (
        set(session.scalars(select(ActivityEventRecord.user_id).distinct()))
        | set(session.scalars(select(TimelogRecord.user_id).distinct()))
        | set(session.scalars(select(WorkItemAssignee.user_id).distinct()))
        | {s.user_id for s in states.values() if s.user_id is not None}
    )
    selected = set(session.scalars(select(User.id).where(User.selected.is_(True))))
    in_teams = store.tracked_team_user_ids(session)

    for user_id in sorted(user_ids_with_data):
        user_states = [states.get(store.state_key(c, user_id)) for c in USER_CATEGORIES]
        if user_id not in selected:
            if user_id in in_teams:
                # History stays; only the dashboard cache expires once it is no longer updated.
                dashboard = [states.get(store.state_key(c, user_id)) for c in DASHBOARD_CATEGORIES]
                if not any(_recent(state, cutoff) for state in dashboard):
                    _purge_dashboard_cache(session, user_id, stats)
            elif not any(_recent(state, cutoff) for state in user_states):
                _purge_user(session, user_id, stats)
            continue
        activity_state = states.get(store.state_key("activity", user_id))
        if _recent(activity_state, cutoff):
            stats.events += _trim_events(session, user_id, visible_from)
        elif activity_state is not None:
            stats.kept_last_known_good.append(f"activity:{user_id}")
        timelog_state = states.get(store.state_key("timelogs", user_id))
        if _recent(timelog_state, cutoff):
            result = session.execute(
                delete(TimelogRecord).where(
                    TimelogRecord.user_id == user_id, TimelogRecord.spent_at < visible_from
                )
            )
            stats.timelogs += int(result.rowcount)  # type: ignore[attr-defined]
        elif timelog_state is not None:
            stats.kept_last_known_good.append(f"timelogs:{user_id}")

    # The calendar and the history only ever cover 53 weeks; older days are never needed again.
    oldest_day = (now - timedelta(days=CONTRIBUTION_DAYS_KEPT)).date()
    for day_model, attr in (
        (ContributionDay, "contribution_days"),
        (ActivityDaily, "history_days"),
        (TimelogDaily, "history_days"),
    ):
        _count(stats, attr, session.execute(delete(day_model).where(day_model.day < oldest_day)))
    oldest = now - timedelta(days=CONTRIBUTION_DAYS_KEPT)
    _count(stats, "outcomes", session.execute(delete(Outcome).where(Outcome.finished_at < oldest)))

    stats.work_items += store.delete_orphan_work_items(session)

    used = exists().where(WorkItemRecord.milestone_id == Milestone.id)
    _count(
        stats,
        "milestones",
        session.execute(delete(Milestone).where(Milestone.seen_at < cutoff, ~used)),
    )

    referenced = or_(
        exists().where(WorkItemRecord.project_id == Project.id),
        exists().where(ActivityEventRecord.project_id == Project.id),
        exists().where(TimelogRecord.project_id == Project.id),
        exists().where(ActivityDaily.project_id == Project.id),
        exists().where(TimelogDaily.project_id == Project.id),
        exists().where(Outcome.project_id == Project.id),
    )
    result = session.execute(delete(Project).where(Project.refreshed_at < cutoff, ~referenced))
    stats.projects += int(result.rowcount)  # type: ignore[attr-defined]

    latest_runs = select(func.max(SyncRun.started_at)).group_by(SyncRun.kind)
    result = session.execute(
        delete(SyncRun).where(
            SyncRun.started_at < cutoff,
            SyncRun.status != "running",
            SyncRun.started_at.not_in(latest_runs),
        )
    )
    stats.runs += int(result.rowcount)  # type: ignore[attr-defined]

    error_cutoff = now - timedelta(days=settings.error_retention_days)
    result = session.execute(
        delete(ErrorRecord).where(
            ErrorRecord.resolved_at.is_not(None), ErrorRecord.resolved_at < error_cutoff
        )
    )
    stats.errors += int(result.rowcount)  # type: ignore[attr-defined]
    return stats


def vacuum_if_needed(engine: Engine) -> bool:
    """Reclaim free pages once enough have accumulated (VACUUM cannot run in a transaction)."""
    with engine.connect() as connection:
        free_pages = int(connection.execute(text("PRAGMA freelist_count")).scalar() or 0)
    if free_pages < VACUUM_FREE_PAGES:
        return False
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM"))
    return True

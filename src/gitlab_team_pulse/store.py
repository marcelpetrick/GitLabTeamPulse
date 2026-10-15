"""Persistence helpers operating on a caller-owned SQLAlchemy session.

Callers own the transaction: every function here only stages changes, so a sync step can
write its data, its sync state and the bumped ``data_version`` in one atomic commit.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.orm import Session

from gitlab_team_pulse.gitlab.models import (
    ActivityEvent,
    GitLabProject,
    GitLabUser,
    MilestoneRef,
    Timelog,
    WorkItem,
)
from gitlab_team_pulse.models import (
    ActivityDaily,
    ActivityEventRecord,
    AppState,
    ContributionDay,
    ErrorRecord,
    Milestone,
    Outcome,
    Project,
    SyncRun,
    SyncState,
    Team,
    TeamMember,
    TeamScope,
    TimelogDaily,
    TimelogRecord,
    User,
    WorkItemAssignee,
    WorkItemRecord,
)

DATA_VERSION = "data_version"
USERS_VERSION = "users_version"
TEAMS_VERSION = "teams_version"
# Per-user history categories, refreshed hourly for selected users and team members.
HISTORY_CATEGORIES = ("contributions", "timelog_history", "outcomes")
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


def users_version(session: Session) -> int:
    return int(get_value(session, USERS_VERSION) or 0)


def bump_users_version(session: Session) -> int:
    """Separate counter for the directory/selection, so People tabs do not refetch the
    whole directory every time a selected user's activity changes."""
    version = users_version(session) + 1
    set_value(session, USERS_VERSION, str(version))
    return version


def teams_version(session: Session) -> int:
    return int(get_value(session, TEAMS_VERSION) or 0)


def bump_teams_version(session: Session) -> int:
    """Changes when a team is created, edited or deleted."""
    version = teams_version(session) + 1
    set_value(session, TEAMS_VERSION, str(version))
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


# ---------------------------------------------------------------------- users


def upsert_users(session: Session, users: list[GitLabUser], now: datetime) -> tuple[int, int]:
    """Insert new accounts and refresh metadata; selection state is never touched.

    Accounts missing from a response are kept: one incomplete listing must not delete users.
    """
    existing = {user.id: user for user in session.scalars(select(User))}
    added = updated = 0
    for remote in users:
        local = existing.get(remote.id)
        if local is None:
            local = User(id=remote.id, selected=False, first_seen_at=now)
            session.add(local)
            existing[remote.id] = local
            added += 1
        else:
            updated += 1
        local.username = remote.username
        local.name = remote.name
        local.avatar_url = remote.avatar_url
        local.web_url = remote.web_url
        local.created_at = remote.created_at
        local.state = remote.state
        local.account_type = remote.account_type
        local.last_seen_at = now
    return added, updated


def list_users(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.created_at.desc(), User.id.desc())))


def selected_users(session: Session) -> list[User]:
    return list(session.scalars(select(User).where(User.selected.is_(True)).order_by(User.name)))


def user_counts(session: Session) -> tuple[int, int]:
    total = session.scalar(select(func.count()).select_from(User)) or 0
    selected = session.scalar(select(func.count()).select_from(User).where(User.selected.is_(True)))
    return int(total), int(selected or 0)


def set_selected(session: Session, user_id: int, selected: bool, now: datetime) -> User | None:
    """Change the global selection; returns None for unknown users."""
    user = session.get(User, user_id)
    if user is None:
        return None
    if user.selected != selected:
        user.selected = selected
        user.selected_at = now if selected else None
    return user


# ---------------------------------------------------------------------- work items


def replace_user_work(
    session: Session,
    user_id: int,
    items: list[WorkItem],
    now: datetime,
    *,
    keep_kinds: frozenset[str] = frozenset(),
) -> None:
    """Make ``items`` the complete current work snapshot for one user (all states).

    Links to items of ``keep_kinds`` are left untouched: their last known good state is kept
    when that kind could not be fetched this time.
    """
    by_key: dict[tuple[str, int], WorkItem] = {(i.kind, i.gitlab_id): i for i in items}
    existing: dict[tuple[str, int], WorkItemRecord] = {}
    ids = {gitlab_id for _, gitlab_id in by_key}
    if ids:
        for found in session.scalars(
            select(WorkItemRecord).where(WorkItemRecord.gitlab_id.in_(ids))
        ):
            existing[(found.kind, found.gitlab_id)] = found
    for key, item in by_key.items():
        record = existing.get(key)
        if record is None:
            record = WorkItemRecord(kind=item.kind, gitlab_id=item.gitlab_id)
            session.add(record)
            existing[key] = record
        record.iid = item.iid
        record.project_id = item.project_id
        record.reference = item.reference
        record.title = item.title
        record.state = item.state
        record.issue_type = item.issue_type
        record.labels = list(item.labels)
        record.milestone = item.milestone
        record.priority = item.priority
        record.due_date = item.due_date
        record.created_at = item.created_at
        record.updated_at = item.updated_at
        record.closed_at = item.closed_at
        record.web_url = item.web_url
        record.author = item.author
        record.assignees = list(item.assignees)
        record.draft = item.draft
        record.refreshed_at = now
        record.merged_at = item.merged_at
        record.time_estimate = item.time_estimate
        record.time_spent = item.time_spent
        record.reviewers = list(item.reviewers)
        record.merge_status = item.merge_status
        record.has_conflicts = item.has_conflicts
        record.discussions_resolved = item.discussions_resolved
        record.milestone_id = item.milestone_ref.id if item.milestone_ref else None
    upsert_milestones(session, [i.milestone_ref for i in items if i.milestone_ref], now)
    session.flush()
    stale_links = delete(WorkItemAssignee).where(WorkItemAssignee.user_id == user_id)
    if keep_kinds:
        kept = select(WorkItemRecord.id).where(WorkItemRecord.kind.in_(keep_kinds))
        stale_links = stale_links.where(WorkItemAssignee.work_item_id.not_in(kept))
    session.execute(stale_links)
    links = {(existing[(i.kind, i.gitlab_id)].id, i.relation) for i in items}
    session.add_all(
        WorkItemAssignee(work_item_id=wid, user_id=user_id, relation=rel, observed_at=now)
        for wid, rel in links
    )
    session.flush()
    delete_orphan_work_items(session)


def delete_orphan_work_items(session: Session) -> int:
    linked = exists().where(WorkItemAssignee.work_item_id == WorkItemRecord.id)
    result = session.execute(delete(WorkItemRecord).where(~linked))
    return int(result.rowcount)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------- activity


def add_user_events(
    session: Session, user_id: int, events: list[ActivityEvent], now: datetime
) -> int:
    """Insert events not stored yet (incremental: event IDs are immutable)."""
    ids = {event.id for event in events}
    known = (
        set(session.scalars(select(ActivityEventRecord.id).where(ActivityEventRecord.id.in_(ids))))
        if ids
        else set()
    )
    added = 0
    for event in events:
        if event.id in known:
            continue
        known.add(event.id)
        session.add(
            ActivityEventRecord(
                id=event.id,
                user_id=user_id,
                project_id=event.project_id,
                category=event.category,
                action_name=event.action_name,
                target_type=event.target_type,
                target_title=event.target_title,
                summary=event.summary,
                occurred_at=event.created_at,
                url_path=event.url_path,
                fetched_at=now,
            )
        )
        added += 1
    return added


# ---------------------------------------------------------------------- timelogs


def replace_user_timelogs(
    session: Session, user_id: int, logs: list[Timelog], window_start: datetime, now: datetime
) -> None:
    """Replace one user's time entries inside the window with the freshly fetched set."""
    ids = {log.id for log in logs}
    session.execute(
        delete(TimelogRecord).where(
            TimelogRecord.user_id == user_id, TimelogRecord.spent_at >= window_start
        )
    )
    if ids:
        session.execute(delete(TimelogRecord).where(TimelogRecord.id.in_(ids)))
    session.add_all(
        TimelogRecord(
            id=log.id,
            user_id=user_id,
            project_id=log.project_id,
            project_path=log.project_path,
            spent_at=log.spent_at,
            seconds=log.seconds,
            summary=log.summary,
            target_kind=log.target_kind,
            target_iid=log.target_iid,
            target_title=log.target_title,
            web_url=log.web_url,
            fetched_at=now,
        )
        for log in {log.id: log for log in logs}.values()
    )


# ---------------------------------------------------------------------- projects


def projects_needing_refresh(session: Session, ids: set[int], stale_before: datetime) -> set[int]:
    if not ids:
        return set()
    fresh = set(
        session.scalars(
            select(Project.id).where(Project.id.in_(ids), Project.refreshed_at >= stale_before)
        )
    )
    return ids - fresh


def upsert_project(session: Session, project: GitLabProject, now: datetime) -> None:
    record = session.get(Project, project.id)
    if record is None:
        record = Project(id=project.id)
        session.add(record)
    record.name = project.name
    record.path_with_namespace = project.path_with_namespace
    record.web_url = project.web_url
    record.refreshed_at = now


# ---------------------------------------------------------------------- contributions


def replace_contribution_days(
    session: Session, user_id: int, counts: dict[date, int], from_day: date
) -> None:
    """Replace one user's daily contribution counts from ``from_day`` onwards."""
    session.execute(
        delete(ContributionDay).where(
            ContributionDay.user_id == user_id, ContributionDay.day >= from_day
        )
    )
    session.add_all(
        ContributionDay(user_id=user_id, day=day, count=count)
        for day, count in sorted(counts.items())
        if day >= from_day and count > 0
    )


# ---------------------------------------------------------------------- milestones


def upsert_milestones(session: Session, refs: list[MilestoneRef], now: datetime) -> None:
    """Remember milestone metadata seen on work items; statistics are filled separately."""
    unique = {ref.id: ref for ref in refs}
    if not unique:
        return
    existing = {m.id: m for m in session.scalars(select(Milestone).where(Milestone.id.in_(unique)))}
    for ref in unique.values():
        record = existing.get(ref.id)
        if record is None:
            record = Milestone(id=ref.id)
            session.add(record)
        record.iid = ref.iid
        record.project_id = ref.project_id
        record.group_id = ref.group_id
        record.title = ref.title
        record.state = ref.state
        record.start_date = ref.start_date
        record.due_date = ref.due_date
        record.web_url = ref.web_url
        record.seen_at = now


OPEN_STATES = frozenset({"opened", "open", "locked"})


def milestone_work(session: Session, user_ids: list[int]) -> list[tuple[int, int, int]]:
    """(milestone ID, user ID, work item ID) of the users' open, assigned, milestoned items."""
    if not user_ids:
        return []
    rows = session.execute(
        select(WorkItemRecord.milestone_id, WorkItemAssignee.user_id, WorkItemRecord.id)
        .join(WorkItemAssignee, WorkItemAssignee.work_item_id == WorkItemRecord.id)
        .where(
            WorkItemAssignee.user_id.in_(user_ids),
            WorkItemAssignee.relation == "assignee",
            WorkItemRecord.milestone_id.is_not(None),
            WorkItemRecord.state.in_(OPEN_STATES),
        )
    ).all()
    return [(mid, uid, wid) for mid, uid, wid in rows if mid is not None]


def relevant_milestones(session: Session, user_ids: list[int], limit: int) -> list[Milestone]:
    """Active milestones of the users' open assigned work, soonest due first, at most ``limit``.

    The single rule for both the hourly progress sync and the dashboard, so every milestone
    that is shown is also one whose progress gets computed.
    """
    ids = {mid for mid, _, _ in milestone_work(session, user_ids)}
    if not ids:
        return []
    found = list(
        session.scalars(select(Milestone).where(Milestone.id.in_(ids), Milestone.state == "active"))
    )
    found.sort(key=lambda m: (m.due_date is None, m.due_date or date.max, m.title))
    return found[:limit]


def milestone_stats(issues: list[WorkItem], today: date) -> dict[str, object]:
    """Progress of a milestone computed from its issues (GitLab Free has no burndown API)."""
    open_items = [i for i in issues if i.state not in ("closed", "merged")]
    closed = [i for i in issues if i.state == "closed"]
    per_assignee: dict[str, int] = {}
    for item in open_items:
        for name in item.assignees or ("",):
            per_assignee[name] = per_assignee.get(name, 0) + 1
    return {
        "total": len(issues),
        "closed": len(closed),
        "estimate": sum(i.time_estimate for i in issues),
        "spent": sum(i.time_spent for i in issues),
        "remaining": sum(max(i.time_estimate - i.time_spent, 0) for i in open_items),
        "open_unestimated": sum(1 for i in open_items if i.time_estimate == 0),
        "open_overdue": sum(1 for i in open_items if i.due_date is not None and i.due_date < today),
        "open_by_assignee": per_assignee,
        "closed_dates": sorted(i.closed_at.date().isoformat() for i in closed if i.closed_at),
    }


def set_milestone_stats(
    session: Session, milestone_id: int, stats: dict[str, object], now: datetime
) -> None:
    record = session.get(Milestone, milestone_id)
    if record is not None:
        record.stats = stats
        record.stats_at = now


# ---------------------------------------------------------------------- history


def replace_activity_daily(
    session: Session, user_id: int, counts: dict[tuple[date, int, str], int], from_day: date
) -> None:
    """Replace one user's daily history counters from ``from_day`` onwards."""
    session.execute(
        delete(ActivityDaily).where(ActivityDaily.user_id == user_id, ActivityDaily.day >= from_day)
    )
    session.add_all(
        ActivityDaily(user_id=user_id, day=day, project_id=project_id, metric=metric, value=value)
        for (day, project_id, metric), value in sorted(counts.items())
        if day >= from_day and value > 0
    )


def replace_timelog_daily(
    session: Session,
    user_id: int,
    seconds: dict[tuple[date, int], tuple[str | None, int]],
    from_day: date,
) -> None:
    """Replace one user's logged seconds per day and project from ``from_day`` onwards."""
    session.execute(
        delete(TimelogDaily).where(TimelogDaily.user_id == user_id, TimelogDaily.day >= from_day)
    )
    session.add_all(
        TimelogDaily(user_id=user_id, day=day, project_id=pid, project_path=path, seconds=value)
        for (day, pid), (path, value) in sorted(seconds.items())
        if day >= from_day and value > 0
    )


def apply_outcomes(session: Session, user_id: int, items: list[WorkItem]) -> None:
    """Upsert merged MRs and closed issues; drop items that are no longer finished."""
    finished: dict[tuple[str, int], tuple[WorkItem, datetime]] = {}
    unfinished: set[tuple[str, int]] = set()
    for item in items:
        key: tuple[str, int] = (item.kind, item.gitlab_id)
        done = "merged" if item.kind == "merge_request" else "closed"
        when = item.merged_at if item.kind == "merge_request" else item.closed_at
        if item.state == done and when is not None:
            finished[key] = (item, when)
        else:
            unfinished.add(key)
    keys = set(finished) | unfinished
    if not keys:
        return
    existing = {
        (o.kind, o.gitlab_id): o
        for o in session.scalars(
            select(Outcome).where(
                Outcome.user_id == user_id, Outcome.gitlab_id.in_({gid for _, gid in keys})
            )
        )
    }
    for gone in unfinished:
        if gone in existing:
            session.delete(existing[gone])
    for done_key, (item, when) in finished.items():
        record = existing.get(done_key)
        if record is None:
            record = Outcome(user_id=user_id, kind=item.kind, gitlab_id=item.gitlab_id)
            session.add(record)
        record.project_id = item.project_id
        record.iid = item.iid
        record.reference = item.reference
        record.title = item.title
        record.web_url = item.web_url
        record.created_at = item.created_at
        record.finished_at = when
        record.time_estimate = item.time_estimate
        record.time_spent = item.time_spent


# ---------------------------------------------------------------------- teams


def list_teams(session: Session) -> list[Team]:
    return list(session.scalars(select(Team).order_by(func.lower(Team.name))))


def team_members(session: Session, team_ids: list[int] | None = None) -> dict[int, list[int]]:
    statement = select(TeamMember.team_id, TeamMember.user_id)
    if team_ids is not None:
        statement = statement.where(TeamMember.team_id.in_(team_ids))
    result: dict[int, list[int]] = {}
    for team_id, user_id in session.execute(statement):
        result.setdefault(team_id, []).append(user_id)
    return result


def team_scopes(session: Session, team_ids: list[int] | None = None) -> dict[int, list[str]]:
    statement = select(TeamScope.team_id, TeamScope.path).order_by(TeamScope.path)
    if team_ids is not None:
        statement = statement.where(TeamScope.team_id.in_(team_ids))
    result: dict[int, list[str]] = {}
    for team_id, path in session.execute(statement):
        result.setdefault(team_id, []).append(path)
    return result


def tracked_team_user_ids(session: Session) -> set[int]:
    """Everyone in any team (history is kept for them even when not on the dashboard)."""
    return set(session.scalars(select(TeamMember.user_id).distinct()))


def save_team(
    session: Session,
    team: Team | None,
    *,
    name: str,
    description: str,
    member_ids: list[int],
    scopes: list[str],
    now: datetime,
) -> Team:
    """Create or replace a team's name, description, members and scope."""
    if team is None:
        team = Team(name=name, description=description, created_at=now, updated_at=now)
        session.add(team)
        session.flush()
    team.name = name
    team.description = description
    team.updated_at = now
    session.execute(delete(TeamMember).where(TeamMember.team_id == team.id))
    session.execute(delete(TeamScope).where(TeamScope.team_id == team.id))
    session.add_all(TeamMember(team_id=team.id, user_id=uid) for uid in sorted(set(member_ids)))
    session.add_all(TeamScope(team_id=team.id, path=path) for path in sorted(set(scopes)))
    return team


def apply_selection(session: Session, user_ids: set[int], now: datetime) -> tuple[set[int], int]:
    """Make exactly ``user_ids`` the selection; returns (newly selected IDs, deselected count)."""
    added: set[int] = set()
    removed = 0
    for user in session.scalars(
        select(User).where(User.selected.is_(True) | User.id.in_(user_ids))
    ):
        wanted = user.id in user_ids
        if wanted and not user.selected:
            added.add(user.id)
        elif not wanted and user.selected:
            removed += 1
        set_selected(session, user.id, wanted, now)
    return added, removed

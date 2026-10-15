"""Read model: assembles API payloads from the SQLite cache with a fixed number of queries.

Every payload carries freshness metadata next to the data, so the browser cannot render
stale content as if it were current. GitLab-originated URLs are only passed through when they
point at the configured GitLab instance.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gitlab_team_pulse import __version__, store
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.freshness import FreshnessInfo, classify
from gitlab_team_pulse.models import (
    ActivityEventRecord,
    ContributionDay,
    ErrorRecord,
    Project,
    SyncState,
    TimelogRecord,
    User,
    WorkItemAssignee,
    WorkItemRecord,
)
from gitlab_team_pulse.sync import (
    HISTORY_CATEGORIES,
    RECENT_EVENTS,
    RuntimeState,
    contribution_grid_start,
)

CATEGORIES = ("push", "comment", "issue", "merge_request", "other")
USER_DATASETS = ("work", "activity", "timelogs")
# Datasets whose errors map to their own per-user state; the calendar has its own pill, so it
# is not part of the combined per-user freshness above.
ERROR_DATASETS = (*USER_DATASETS, *HISTORY_CATEGORIES)
OPEN_STATES = store.OPEN_STATES
ACTIVITY_PREVIEW = 5
# detailed_merge_status values that mean "this cannot be merged as it is" (GitLab Free).
MERGE_PROBLEMS = {
    "conflict": "conflict",
    "need_rebase": "need_rebase",
    "ci_must_pass": "pipeline",
    "discussions_not_resolved": "threads",
    "requested_changes": "changes_requested",
    "blocked_status": "blocked",
}
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def safe_url(url: str | None, settings: Settings) -> str | None:
    """Allow only links into the configured GitLab instance (relative paths are anchored)."""
    base = settings.gitlab_url
    if not url or not base:
        return None
    if url.startswith("/") and not url.startswith("//"):
        return f"{base}{url}"
    if url == base or url.startswith(f"{base}/"):
        return url
    return None


def safe_avatar(url: str | None, settings: Settings) -> str | None:
    """Avatars may live on a Gravatar-style host; only http(s) images are passed through."""
    if not url:
        return None
    if url.startswith("/") and not url.startswith("//"):
        return safe_url(url, settings)
    return url if url.startswith(("https://", "http://")) else None


def user_payload(user: User, settings: Settings) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "name": user.name,
        "avatar_url": safe_avatar(user.avatar_url, settings),
        "web_url": safe_url(user.web_url, settings),
        "created_at": user.created_at,
        "state": user.state,
        "account_type": user.account_type,
        "selected": user.selected,
    }


def _dataset_freshness(
    state: SyncState | None,
    *,
    running: bool,
    interval: int,
    settings: Settings,
    now: datetime,
) -> FreshnessInfo:
    return classify(
        last_success_at=state.last_success_at if state else None,
        last_attempt_at=state.last_attempt_at if state else None,
        last_status=state.status if state else None,
        running=running,
        interval_seconds=interval,
        grace_seconds=settings.stale_grace_seconds,
        now=now,
        message=state.last_error if state else None,
    )


def user_freshness(
    states: dict[str, SyncState],
    user_id: int,
    runtime: RuntimeState,
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    """Per-user freshness: the oldest dataset success, error if any dataset failed."""
    scope = runtime.selected_scope
    running = "selected" in runtime.running and (scope is None or user_id in scope)
    interval = settings.selected_refresh_interval_seconds
    categories: dict[str, Any] = {}
    successes: list[datetime | None] = []
    attempts: list[datetime] = []
    errors: list[str] = []
    for dataset in USER_DATASETS:
        state = states.get(store.state_key(dataset, user_id))
        info = _dataset_freshness(
            state, running=running, interval=interval, settings=settings, now=now
        )
        categories[dataset] = info.as_dict()
        successes.append(info.last_success_at)
        if info.last_attempt_at:
            attempts.append(info.last_attempt_at)
        if info.error and state and state.last_error:
            errors.append(f"{dataset}: {state.last_error}")
    oldest = None if any(s is None for s in successes) else min(s for s in successes if s)
    combined = classify(
        last_success_at=oldest,
        last_attempt_at=max(attempts) if attempts else None,
        last_status="error" if errors else ("ok" if oldest else None),
        running=running,
        interval_seconds=interval,
        grace_seconds=settings.stale_grace_seconds,
        now=now,
        message="; ".join(errors) or None,
    )
    return {**combined.as_dict(), "categories": categories}


def _project_payload(
    project_id: int | None, projects: dict[int, Project], settings: Settings
) -> dict[str, Any] | None:
    if project_id is None:
        return None
    project = projects.get(project_id)
    if project is None:
        return {"id": project_id, "name": f"Project #{project_id}", "path": None, "web_url": None}
    return {
        "id": project.id,
        "name": project.name,
        "path": project.path_with_namespace,
        "web_url": safe_url(project.web_url, settings),
    }


def _group_payload(reference: str) -> dict[str, Any] | None:
    """Epics live in groups, not projects: show the group path from the reference."""
    group = reference.split("&", 1)[0] if "&" in reference else ""
    return {"id": None, "name": group, "path": group, "web_url": None} if group else None


ATTENTION_FIELDS = (
    "id",
    "kind",
    "reference",
    "title",
    "state",
    "draft",
    "web_url",
    "project",
    "created_at",
    "updated_at",
    "due_date",
    "milestone",
    "reviewers",
)
ATTENTION_CODES = (
    "stalled",
    "overdue",
    "due_soon",
    "review",
    "cannot_merge",
    "no_reviewer",
    "outside_reviewer",
)


def merge_problems(item: WorkItemRecord) -> list[str]:
    """Why an open MR cannot be merged as it is, from fields GitLab Free provides."""
    if item.kind != "merge_request" or item.state not in OPEN_STATES:
        return []
    problems = []
    if item.has_conflicts or item.merge_status == "conflict":
        problems.append("conflict")
    mapped = MERGE_PROBLEMS.get(item.merge_status or "")
    if mapped and mapped not in problems:
        problems.append(mapped)
    if item.discussions_resolved is False and "threads" not in problems:
        problems.append("threads")
    return problems


def local_days(settings: Settings, now: datetime) -> list[date]:
    today = now.astimezone(settings.tz).date()
    return [today - timedelta(days=offset) for offset in range(settings.activity_days - 1, -1, -1)]


def _event_url(
    event: ActivityEventRecord, projects: dict[int, Project], settings: Settings
) -> str | None:
    project = projects.get(event.project_id) if event.project_id is not None else None
    base = safe_url(project.web_url, settings) if project else None
    if base is None:
        return None
    return f"{base}{event.url_path}" if event.url_path else base


class DashboardReader:
    """Loads everything for a set of users in a handful of batched queries."""

    def __init__(self, session: Session, settings: Settings, runtime: RuntimeState, now: datetime):
        self.session = session
        self.settings = settings
        self.runtime = runtime
        self.now = now

    def _projects(self, ids: set[int]) -> dict[int, Project]:
        if not ids:
            return {}
        return {p.id: p for p in self.session.scalars(select(Project).where(Project.id.in_(ids)))}

    def _states(self, user_ids: list[int]) -> dict[str, SyncState]:
        rows = self.session.scalars(select(SyncState).where(SyncState.user_id.in_(user_ids)))
        return {state.key: state for state in rows}

    def work(self, user_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        rows = self.session.execute(
            select(WorkItemAssignee.user_id, WorkItemAssignee.relation, WorkItemRecord)
            .join(WorkItemRecord, WorkItemRecord.id == WorkItemAssignee.work_item_id)
            .where(WorkItemAssignee.user_id.in_(user_ids))
            .order_by(WorkItemRecord.updated_at.desc(), WorkItemRecord.id.desc())
        ).all()
        projects = self._projects({r[2].project_id for r in rows if r[2].project_id is not None})
        grouped: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
        for user_id, relation, item in rows:
            existing = grouped[user_id].get(item.id)
            if existing is not None:
                existing["relations"].append(relation)
                continue
            grouped[user_id][item.id] = {
                "id": item.id,
                "kind": item.kind,
                "reference": item.reference,
                "iid": item.iid,
                "title": item.title,
                "state": item.state,
                "issue_type": item.issue_type,
                "labels": item.labels,
                "milestone": item.milestone,
                "priority": item.priority,
                "due_date": item.due_date,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "closed_at": item.closed_at,
                "web_url": safe_url(item.web_url, self.settings),
                "author": item.author,
                "assignees": item.assignees,
                "draft": item.draft,
                "relations": [relation],
                "project": _project_payload(item.project_id, projects, self.settings)
                or _group_payload(item.reference),
                "merged_at": item.merged_at,
                "time_estimate": item.time_estimate,
                "time_spent": item.time_spent,
                "reviewers": item.reviewers,
                "merge_status": item.merge_status,
                "merge_problems": merge_problems(item),
                "milestone_id": item.milestone_id,
                "stalled_days": self.stalled_days(item),
            }
        return {uid: list(items.values()) for uid, items in grouped.items()}

    def stalled_days(self, item: WorkItemRecord) -> int | None:
        """Days without any update for open items past the threshold, else None."""
        if item.state not in OPEN_STATES:
            return None
        days = (self.now - item.updated_at).days
        return days if days >= self.settings.stalled_days else None

    def today(self) -> date:
        return self.now.astimezone(self.settings.tz).date()

    def attention(self, users: list[User], work: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
        """Open items across the monitored people that need someone to act, one row per item.

        Reasons: stalled (no update for ``stalled_days``), overdue, due soon, waiting for review,
        cannot merge, and MRs without a reviewer among the monitored people.
        """
        today = self.today()
        monitored = {u.username for u in users}
        rows: dict[int, dict[str, Any]] = {}
        for user in users:
            for item in work.get(user.id, []):
                if item["state"] not in OPEN_STATES:
                    continue
                row = rows.get(item["id"])
                if row is None:
                    row = rows[item["id"]] = {
                        **{k: item[k] for k in ATTENTION_FIELDS},
                        "people": [],
                        "reasons": {},
                    }
                row["people"].append(
                    {"username": user.username, "name": user.name, "relations": item["relations"]}
                )
                self._reasons(row["reasons"], item, monitored, today)
        items = []
        for row in rows.values():
            if not row["reasons"]:
                continue
            row["reasons"] = sorted(
                row["reasons"].values(), key=lambda r: (SEVERITY_ORDER[r["severity"]], r["code"])
            )
            row["severity"] = row["reasons"][0]["severity"]
            items.append(row)
        items.sort(
            key=lambda r: (SEVERITY_ORDER[r["severity"]], r["updated_at"] or self.now, r["id"])
        )
        counts: dict[str, int] = dict.fromkeys(ATTENTION_CODES, 0)
        for row in items:
            for reason in row["reasons"]:
                counts[reason["code"]] += 1
        return {
            "stalled_days": self.settings.stalled_days,
            "due_soon_days": self.settings.due_soon_days,
            "counts": counts,
            "items": items,
        }

    def _reasons(
        self,
        reasons: dict[str, dict[str, Any]],
        item: dict[str, Any],
        monitored: set[str],
        today: date,
    ) -> None:
        relations = item["relations"]
        is_mr = item["kind"] == "merge_request"
        if "assignee" in relations:
            if item["stalled_days"] is not None:
                reasons["stalled"] = {
                    "code": "stalled",
                    "severity": "error",
                    "days": item["stalled_days"],
                }
            due = item["due_date"]
            if due is not None and due < today:
                reasons["overdue"] = {
                    "code": "overdue",
                    "severity": "error",
                    "days": (today - due).days,
                }
            elif due is not None and (due - today).days <= self.settings.due_soon_days:
                reasons["due_soon"] = {
                    "code": "due_soon",
                    "severity": "warning",
                    "days": (due - today).days,
                }
            if is_mr and not item["draft"]:
                reviewers = set(item["reviewers"])
                if not reviewers:
                    reasons["no_reviewer"] = {"code": "no_reviewer", "severity": "warning"}
                elif not reviewers & monitored:
                    reasons["outside_reviewer"] = {
                        "code": "outside_reviewer",
                        "severity": "info",
                        "reviewers": sorted(reviewers),
                    }
        if is_mr and not item["draft"]:
            if "reviewer" in relations:
                created = item["created_at"] or item["updated_at"]
                age = (self.now - created).days
                reasons["review"] = {
                    "code": "review",
                    "severity": "warning" if age >= 2 else "info",
                    "days": age,
                    "idle_days": (self.now - item["updated_at"]).days,
                }
            if item["merge_problems"]:
                reasons["cannot_merge"] = {
                    "code": "cannot_merge",
                    "severity": "warning",
                    "problems": item["merge_problems"],
                }

    def milestones(self, users: list[User]) -> list[dict[str, Any]]:
        """Active milestones of monitored people's open items, soonest due first."""
        ids = [u.id for u in users]
        mine: dict[int, set[int]] = defaultdict(set)
        people: dict[int, set[int]] = defaultdict(set)
        for milestone_id, user_id, item_id in store.milestone_work(self.session, ids):
            mine[milestone_id].add(item_id)
            people[milestone_id].add(user_id)
        found = store.relevant_milestones(self.session, ids, self.settings.milestones_max)
        projects = self._projects({m.project_id for m in found if m.project_id is not None})
        names = {u.id: u.name for u in users}
        today = self.today()
        return [
            {
                "id": m.id,
                "title": m.title,
                "web_url": safe_url(m.web_url, self.settings),
                "project": _project_payload(m.project_id, projects, self.settings),
                "group_id": m.group_id,
                "start_date": m.start_date,
                "due_date": m.due_date,
                "days_left": (m.due_date - today).days if m.due_date else None,
                "monitored_open": len(mine[m.id]),
                "monitored_people": sorted(names[uid] for uid in people[m.id]),
                "stats": m.stats,
                "stats_at": m.stats_at,
            }
            for m in found
        ]

    def activity(self, user_ids: list[int]) -> dict[int, dict[str, Any]]:
        events = list(
            self.session.scalars(
                select(ActivityEventRecord)
                .where(ActivityEventRecord.user_id.in_(user_ids))
                .order_by(ActivityEventRecord.occurred_at.desc(), ActivityEventRecord.id.desc())
            )
        )
        projects = self._projects({e.project_id for e in events if e.project_id is not None})
        days = local_days(self.settings, self.now)
        result: dict[int, dict[str, Any]] = {}
        for user_id in user_ids:
            mine = [e for e in events if e.user_id == user_id]
            buckets = {day: dict.fromkeys(CATEGORIES, 0) for day in days}
            for event in mine:
                day = event.occurred_at.astimezone(self.settings.tz).date()
                if day in buckets:
                    buckets[day][event.category if event.category in CATEGORIES else "other"] += 1
            result[user_id] = {
                "recent": [
                    {
                        "id": e.id,
                        "occurred_at": e.occurred_at,
                        "category": e.category,
                        "action_name": e.action_name,
                        "target_type": e.target_type,
                        "summary": e.summary,
                        "project": _project_payload(e.project_id, projects, self.settings),
                        "web_url": _event_url(e, projects, self.settings),
                    }
                    for e in mine[:RECENT_EVENTS]
                ],
                "days": [
                    {"date": day, "total": sum(counts.values()), **counts}
                    for day, counts in buckets.items()
                ],
            }
        return result

    def time(self, user_ids: list[int]) -> dict[int, dict[str, Any]]:
        days = local_days(self.settings, self.now)
        logs = list(
            self.session.scalars(
                select(TimelogRecord)
                .where(TimelogRecord.user_id.in_(user_ids))
                .order_by(TimelogRecord.spent_at.desc(), TimelogRecord.id.desc())
            )
        )
        result: dict[int, dict[str, Any]] = {}
        for user_id in user_ids:
            per_day = dict.fromkeys(days, 0)
            per_project: dict[str, int] = defaultdict(int)
            entries = []
            for log in (t for t in logs if t.user_id == user_id):
                day = log.spent_at.astimezone(self.settings.tz).date()
                if day not in per_day:
                    continue
                per_day[day] += log.seconds
                per_project[log.project_path or "Unknown project"] += log.seconds
                entries.append(
                    {
                        "id": log.id,
                        "spent_at": log.spent_at,
                        "date": day,
                        "seconds": log.seconds,
                        "summary": log.summary,
                        "target_kind": log.target_kind,
                        "target_iid": log.target_iid,
                        "target_title": log.target_title,
                        "project_path": log.project_path,
                        "web_url": safe_url(log.web_url, self.settings),
                    }
                )
            result[user_id] = {
                "total_seconds": sum(per_day.values()),
                "days": [{"date": day, "seconds": seconds} for day, seconds in per_day.items()],
                "projects": [
                    {"path": path, "seconds": seconds}
                    for path, seconds in sorted(per_project.items(), key=lambda kv: -kv[1])
                ],
                "entries": entries,
            }
        return result

    def contributions(
        self, user_ids: list[int], states: dict[str, SyncState]
    ) -> dict[int, dict[str, Any]]:
        """53-week calendar per user: counts aligned to a Sunday-based grid start."""
        today = self.now.astimezone(self.settings.tz).date()
        start = contribution_grid_start(today)
        rows = self.session.execute(
            select(ContributionDay.user_id, ContributionDay.day, ContributionDay.count).where(
                ContributionDay.user_id.in_(user_ids), ContributionDay.day >= start
            )
        ).all()
        size = (today - start).days + 1
        per_user: dict[int, list[tuple[date, int]]] = {}
        for uid, day, count in rows:
            if day <= today:
                per_user.setdefault(uid, []).append((day, count))
        # A refresh only starts on the next selected run after the throttle expires.
        interval = (
            self.settings.contributions_refresh_minutes * 60
            + self.settings.selected_refresh_interval_seconds
        )
        result: dict[int, dict[str, Any]] = {}
        for user_id in user_ids:
            counts = [0] * size
            for day, count in per_user.get(user_id, []):
                counts[(day - start).days] = count
            busiest = max(range(size), key=lambda i: (counts[i], i)) if any(counts) else None
            state = states.get(store.state_key("contributions", user_id))
            freshness = _dataset_freshness(
                state,
                running="selected" in self.runtime.running
                and (self.runtime.selected_scope is None or user_id in self.runtime.selected_scope),
                interval=interval,
                settings=self.settings,
                now=self.now,
            )
            result[user_id] = {
                "start": start,
                "end": today,
                "counts": counts,
                "total": sum(counts),
                "max": max(counts),
                "busiest": {"date": start + timedelta(days=busiest), "count": counts[busiest]}
                if busiest is not None
                else None,
                "freshness": freshness.as_dict(),
            }
        return result

    def overview(self, users: list[User]) -> dict[str, Any]:
        """Cards plus the cross-person attention list and milestones, reading work once."""
        work = self.work([u.id for u in users]) if users else {}
        return {
            "cards": self.cards(users, work),
            "attention": self.attention(users, work),
            "milestones": self.milestones(users),
        }

    def cards(
        self, users: list[User], work: dict[int, list[dict[str, Any]]] | None = None
    ) -> list[dict[str, Any]]:
        ids = [u.id for u in users]
        if not ids:
            return []
        states = self._states(ids)
        work = self.work(ids) if work is None else work
        activity = self.activity(ids)
        time = self.time(ids)
        contributions = self.contributions(ids, states)
        today = self.today()
        cards = []
        for user in users:
            items = work.get(user.id, [])
            user_activity = activity[user.id]
            user_time = time[user.id]
            cards.append(
                {
                    "user": user_payload(user, self.settings),
                    "freshness": user_freshness(
                        states, user.id, self.runtime, self.settings, self.now
                    ),
                    "counts": {
                        "work_total": len(items),
                        "work_open": sum(1 for i in items if i["state"] in OPEN_STATES),
                        "events_7d": sum(d["total"] for d in user_activity["days"]),
                        "time_seconds_7d": user_time["total_seconds"],
                        "stalled": sum(
                            1
                            for i in items
                            if i["stalled_days"] is not None and "assignee" in i["relations"]
                        ),
                        "overdue": sum(
                            1
                            for i in items
                            if i["state"] in OPEN_STATES
                            and "assignee" in i["relations"]
                            and i["due_date"] is not None
                            and i["due_date"] < today
                        ),
                    },
                    "work": items,
                    "activity": user_activity["recent"],
                    "activity_preview_count": ACTIVITY_PREVIEW,
                    "activity_days": user_activity["days"],
                    "time": user_time,
                    "contributions": contributions[user.id],
                }
            )
        return cards


def global_freshness(
    session: Session, runtime: RuntimeState, settings: Settings, now: datetime
) -> tuple[FreshnessInfo, FreshnessInfo]:
    directory = _dataset_freshness(
        store.find_state(session, "directory"),
        running="directory" in runtime.running,
        interval=settings.user_refresh_interval_seconds,
        settings=settings,
        now=now,
    )
    selected = _dataset_freshness(
        store.find_state(session, "selected"),
        running="selected" in runtime.running,
        interval=settings.selected_refresh_interval_seconds,
        settings=settings,
        now=now,
    )
    return directory, selected


def build_status(
    session: Session,
    settings: Settings,
    runtime: RuntimeState,
    now: datetime,
    *,
    refresh_pending: bool = False,
    next_selected_due: datetime | None = None,
) -> dict[str, Any]:
    """Compact operational state; cheap enough for many polling browser tabs."""
    directory, selected = global_freshness(session, runtime, settings, now)
    known, chosen = store.user_counts(session)
    unresolved = store.unresolved_error_count(session)
    if runtime.running or refresh_pending:
        crawler = "refreshing"
    elif selected.error or directory.error:
        crawler = "error"
    else:
        crawler = "idle"
    if runtime.last_upstream_error:
        upstream = "error"
    elif runtime.last_upstream_ok_at:
        upstream = "ok"
    else:
        upstream = "unknown"
    return {
        "version": __version__,
        "now": now,
        "data_version": store.data_version(session),
        "users_version": store.users_version(session),
        "teams_version": store.teams_version(session),
        "teams": {
            "readonly": settings.teams_readonly,
            "person_breakdown": settings.team_person_breakdown,
        },
        "timezone": settings.timezone,
        "ui_poll_interval_seconds": settings.ui_poll_interval_seconds,
        "crawler": {
            "state": crawler,
            "running": sorted(runtime.running),
            "refresh_pending": refresh_pending,
            "active_run_id": runtime.active_run_id,
            "active_trigger": runtime.active_trigger,
        },
        "directory": directory.as_dict(),
        "selected": selected.as_dict(),
        "freshness": selected.status.value,
        "next_selected_refresh_at": next_selected_due,
        "users": {"known": known, "selected": chosen},
        "errors": {"unresolved": unresolved},
        "upstream": {
            "status": upstream,
            "message": runtime.last_upstream_error,
            "last_ok_at": runtime.last_upstream_ok_at,
        },
        "gitlab": {"configured": settings.gitlab_configured, "url": settings.gitlab_url or None},
        "capabilities": {"epics": runtime.epics},
    }


def error_payloads(session: Session, *, limit: int, include_resolved: bool) -> list[dict[str, Any]]:
    """Diagnostics with the related dataset's last success, i.e. what cached data is shown."""
    records: list[ErrorRecord] = store.list_errors(
        session, limit=limit, include_resolved=include_resolved
    )
    user_ids = {r.user_id for r in records if r.user_id is not None}
    users = (
        {u.id: u for u in session.scalars(select(User).where(User.id.in_(user_ids)))}
        if user_ids
        else {}
    )
    states = {s.key: s for s in session.scalars(select(SyncState))}
    payloads = []
    for record in records:
        if record.user_id is not None and record.operation in ERROR_DATASETS:
            key = store.state_key(record.operation, record.user_id)
        elif record.subsystem == "directory":
            key = "directory"
        else:
            key = "selected"
        state = states.get(key)
        last_success = state.last_success_at if state else None
        user = users.get(record.user_id) if record.user_id is not None else None
        payloads.append(
            {
                "id": record.id,
                "occurred_at": record.occurred_at,
                "first_occurred_at": record.first_occurred_at,
                "occurrences": record.occurrences,
                "severity": record.severity,
                "subsystem": record.subsystem,
                "operation": record.operation,
                "user": {"id": user.id, "username": user.username, "name": user.name}
                if user
                else None,
                "project_id": record.project_id,
                "message": record.message,
                "resolved_at": record.resolved_at,
                "related_dataset": key,
                "last_success_at": last_success,
                "cached_data_shown": last_success is not None,
            }
        )
    return payloads

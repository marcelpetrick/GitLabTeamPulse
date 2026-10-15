"""Teams: named groups of people and their reports, computed from the history aggregates.

The report is about the team's work, not a ranking of people: totals with deltas are team-level
only, the per-person table is factual, alphabetical and switched off unless configured
(``TEAMPULSE_TEAM_PERSON_BREAKDOWN``). Every number says how it is counted.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from gitlab_team_pulse import store
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.dashboard import safe_url, user_payload
from gitlab_team_pulse.models import (
    ActivityDaily,
    Outcome,
    Project,
    Team,
    TimelogDaily,
    User,
)
from gitlab_team_pulse.sync import HISTORY_CATEGORIES, contribution_grid_start

Period = Literal["week", "month", "quarter"]
PERIODS: tuple[Period, ...] = ("week", "month", "quarter")
SPREAD_WEEKS = 12
SPREAD_METRICS = ("mr_approved", "mr_commented")
TOTALS = ("hours", "mrs_merged", "issues_closed", "mrs_opened", "approvals", "review_comments")
ACTIVITY_TOTALS = {
    "mr_opened": "mrs_opened",
    "mr_approved": "approvals",
    "mr_commented": "review_comments",
}
NO_PROJECT = "(no project)"
COUNTING = [
    ("Hours", "Time entries logged in GitLab by team members, by the day they were spent."),
    ("MRs merged", "Merged merge requests authored by a team member (each MR counted once)."),
    (
        "Issues closed",
        "Closed issues currently assigned to a team member (each issue counted once; GitLab "
        "only reports today's assignees).",
    ),
    ("MRs opened", "Merge requests opened by team members (GitLab events)."),
    ("Approvals", "Merge request approvals given by team members (GitLab events)."),
    ("Review comments", "Comments by team members on merge requests (GitLab events)."),
    (
        "MR cycle time",
        "Time from creation to merge of the merged MRs: median and middle half (IQR).",
    ),
    (
        "Estimate vs spent",
        "Time estimate and total time spent of the closed issues that have an estimate; the "
        "spent time includes everybody who logged time on them.",
    ),
    (
        "Knowledge spread",
        f"Team members who authored a merged MR, approved or reviewed MRs in a project during "
        f"the last {SPREAD_WEEKS} weeks. One person means the knowledge sits with one person.",
    ),
    (
        "Not counted",
        "Work outside GitLab (support, meetings, mentoring) and work of people outside the team.",
    ),
]


class TeamError(ValueError):
    """Invalid team input; ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------- payloads


def normalize_scope(path: str) -> str:
    return path.strip().strip("/")


def in_scope(path: str | None, scopes: list[str]) -> bool:
    """A project counts when its path equals a scope or lies below a scoped namespace."""
    if not scopes:
        return True
    if not path:
        return False
    return any(path == scope or path.startswith(f"{scope}/") for scope in scopes)


def team_payloads(session: Session, settings: Settings) -> list[dict[str, Any]]:
    teams = store.list_teams(session)
    ids = [t.id for t in teams]
    members = store.team_members(session, ids)
    scopes = store.team_scopes(session, ids)
    user_ids = {uid for found in members.values() for uid in found}
    users = (
        {u.id: u for u in session.scalars(select(User).where(User.id.in_(user_ids)))}
        if user_ids
        else {}
    )
    return [
        {
            "id": team.id,
            "name": team.name,
            "description": team.description,
            "members": sorted(
                (user_payload(users[uid], settings) for uid in members.get(team.id, [])),
                key=lambda u: (u["name"].lower(), u["username"]),
            ),
            "scopes": scopes.get(team.id, []),
            "created_at": team.created_at,
            "updated_at": team.updated_at,
        }
        for team in teams
    ]


def validate_team(
    session: Session,
    *,
    team_id: int | None,
    name: str,
    member_ids: list[int],
    scopes: list[str],
) -> tuple[str, list[int], list[str]]:
    name = " ".join(name.split())
    if not name:
        raise TeamError("A team needs a name")
    if len(name) > 120:
        raise TeamError("The team name is longer than 120 characters")
    clash = session.scalars(select(Team).where(Team.name == name)).first()
    if clash is not None and clash.id != team_id:
        raise TeamError(f"A team called {name!r} already exists", status=409)
    wanted = set(member_ids)
    known = set(session.scalars(select(User.id).where(User.id.in_(wanted)))) if wanted else set()
    if unknown := wanted - known:
        raise TeamError(f"Unknown users: {', '.join(map(str, sorted(unknown)))}")
    cleaned = sorted({normalize_scope(s) for s in scopes if normalize_scope(s)})
    if len(cleaned) > 50:
        raise TeamError("A team can have at most 50 scope entries")
    if any(len(s) > 512 for s in cleaned):
        raise TeamError("A scope path is longer than 512 characters")
    return name, sorted(wanted), cleaned


# ---------------------------------------------------------------------- periods


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def period_start(period: Period, today: date, offset: int) -> date:
    if period == "week":
        return today - timedelta(days=today.weekday()) - timedelta(weeks=offset)
    months = 1 if period == "month" else 3
    first = date(today.year, today.month - (today.month - 1) % months, 1)
    return _add_months(first, -months * offset)


def period_end(period: Period, start: date) -> date:
    """Exclusive end of the period that starts on ``start``."""
    if period == "week":
        return start + timedelta(days=7)
    return _add_months(start, 1 if period == "month" else 3)


def period_label(period: Period, start: date) -> str:
    if period == "week":
        iso = start.isocalendar()
        return f"Week {iso.week}, {iso.year} ({start:%d %b} to {start + timedelta(days=6):%d %b})"
    if period == "month":
        return f"{start:%B %Y}"
    return f"Q{(start.month - 1) // 3 + 1} {start.year}"


@dataclass(frozen=True)
class Window:
    start: date
    end: date  # exclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def contains(self, day: date) -> bool:
        return self.start <= day < self.end


# ---------------------------------------------------------------------- report


@dataclass
class _Facts:
    """History rows of the team members between two dates, with project paths resolved."""

    timelogs: list[tuple[int, date, str | None, int]]
    activity: list[tuple[int, date, str | None, str, int]]
    outcomes: list[tuple[int, Outcome, date, str | None]]


class TeamReporter:
    def __init__(self, session: Session, settings: Settings, now: datetime) -> None:
        self.session = session
        self.settings = settings
        self.now = now
        self.today = now.astimezone(settings.tz).date()

    def _midnight(self, day: date) -> datetime:
        return datetime.combine(day, datetime.min.time(), self.settings.tz).astimezone(UTC)

    def _facts(self, member_ids: list[int], start: date, end: date) -> _Facts:
        timelog_rows = self.session.execute(
            select(
                TimelogDaily.user_id,
                TimelogDaily.day,
                TimelogDaily.project_id,
                TimelogDaily.project_path,
                TimelogDaily.seconds,
            ).where(
                TimelogDaily.user_id.in_(member_ids),
                TimelogDaily.day >= start,
                TimelogDaily.day < end,
            )
        ).all()
        activity_rows = self.session.execute(
            select(
                ActivityDaily.user_id,
                ActivityDaily.day,
                ActivityDaily.project_id,
                ActivityDaily.metric,
                ActivityDaily.value,
            ).where(
                ActivityDaily.user_id.in_(member_ids),
                ActivityDaily.day >= start,
                ActivityDaily.day < end,
            )
        ).all()
        outcome_rows = list(
            self.session.scalars(
                select(Outcome).where(
                    Outcome.user_id.in_(member_ids),
                    Outcome.finished_at >= self._midnight(start),
                    Outcome.finished_at < self._midnight(end),
                )
            )
        )
        project_ids = {r[2] for r in timelog_rows} | {r[2] for r in activity_rows}
        project_ids |= {o.project_id for o in outcome_rows if o.project_id is not None}
        paths = (
            {
                p.id: p.path_with_namespace
                for p in self.session.scalars(select(Project).where(Project.id.in_(project_ids)))
            }
            if project_ids
            else {}
        )
        tz = self.settings.tz
        return _Facts(
            timelogs=[
                (uid, day, paths.get(pid) or path, seconds)
                for uid, day, pid, path, seconds in timelog_rows
            ],
            activity=[
                (uid, day, paths.get(pid), metric, value)
                for uid, day, pid, metric, value in activity_rows
            ],
            outcomes=[
                (
                    o.user_id,
                    o,
                    o.finished_at.astimezone(tz).date(),
                    paths.get(o.project_id) if o.project_id is not None else None,
                )
                for o in outcome_rows
            ],
        )

    @staticmethod
    def _totals(facts: _Facts, window: Window, scopes: list[str]) -> dict[str, Any]:
        seconds = sum(
            s
            for _, day, path, s in facts.timelogs
            if window.contains(day) and in_scope(path, scopes)
        )
        activity: dict[str, int] = defaultdict(int)
        for _, day, path, metric, value in facts.activity:
            if window.contains(day) and in_scope(path, scopes) and metric in ACTIVITY_TOTALS:
                activity[ACTIVITY_TOTALS[metric]] += value
        merged: dict[int, Outcome] = {}
        closed: dict[int, Outcome] = {}
        for _, outcome, day, path in facts.outcomes:
            if window.contains(day) and in_scope(path, scopes):
                target = merged if outcome.kind == "merge_request" else closed
                target[outcome.gitlab_id] = outcome
        cycle = sorted(
            (o.finished_at - o.created_at).total_seconds() / 3600
            for o in merged.values()
            if o.created_at is not None and o.finished_at >= o.created_at
        )
        estimated = [o for o in closed.values() if o.time_estimate > 0]
        return {
            "hours": round(seconds / 3600, 1),
            "seconds": seconds,
            "mrs_merged": len(merged),
            "issues_closed": len(closed),
            "mrs_opened": activity["mrs_opened"],
            "approvals": activity["approvals"],
            "review_comments": activity["review_comments"],
            "cycle_time_hours": _quartiles(cycle),
            "estimate": {
                "issues": len(closed),
                "with_estimate": len(estimated),
                "estimate_seconds": sum(o.time_estimate for o in estimated),
                "spent_seconds": sum(o.time_spent for o in estimated),
            },
        }

    def report(self, team_id: int, period: Period, offset: int) -> dict[str, Any] | None:
        team = self.session.get(Team, team_id)
        if team is None:
            return None
        member_ids = store.team_members(self.session, [team_id]).get(team_id, [])
        scopes = store.team_scopes(self.session, [team_id]).get(team_id, [])
        start = period_start(period, self.today, offset)
        end = period_end(period, start)
        running = start <= self.today < end
        current = Window(start, self.today + timedelta(days=1) if running else end)
        previous_start = period_start(period, start - timedelta(days=1), 0)
        previous_end = period_end(period, previous_start)
        previous = Window(
            previous_start, min(previous_start + timedelta(days=current.days), previous_end)
        )
        spread_start = self.today - timedelta(weeks=SPREAD_WEEKS) + timedelta(days=1)
        facts = self._facts(
            member_ids,
            min(previous.start, spread_start),
            max(current.end, self.today + timedelta(days=1)),
        )
        users = (
            {u.id: u for u in self.session.scalars(select(User).where(User.id.in_(member_ids)))}
            if member_ids
            else {}
        )
        totals = self._totals(facts, current, scopes)
        before = self._totals(facts, previous, scopes)
        history_from = contribution_grid_start(self.today)
        return {
            "team": {"id": team.id, "name": team.name, "scopes": scopes},
            "period": {
                "kind": period,
                "offset": offset,
                "label": period_label(period, start),
                "start": current.start,
                "end": current.end - timedelta(days=1),
                "period_end": end - timedelta(days=1),
                "running": running,
                "elapsed_days": current.days,
                "length_days": (end - start).days,
                "previous": {
                    "label": period_label(period, previous_start),
                    "start": previous.start,
                    "end": previous.end - timedelta(days=1),
                    "partial": previous.end < previous_end,
                },
                "before_history": start < history_from,
            },
            "totals": totals,
            "previous": before,
            "projects": self._projects(facts, current, scopes),
            "spread": self._spread(facts, users, scopes, spread_start),
            "people": self._people(facts, users, current, scopes)
            if self.settings.team_person_breakdown
            else None,
            "person_breakdown": self.settings.team_person_breakdown,
            "history": self._history(member_ids, users, history_from),
            "counting": [{"metric": m, "rule": r} for m, r in COUNTING],
        }

    @staticmethod
    def _projects(facts: _Facts, window: Window, scopes: list[str]) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"seconds": 0, "mrs": set(), "issues": set(), "events": 0}
        )
        for _, day, path, seconds in facts.timelogs:
            if window.contains(day) and in_scope(path, scopes):
                rows[path or NO_PROJECT]["seconds"] += seconds
        for _, outcome, day, path in facts.outcomes:
            if window.contains(day) and in_scope(path, scopes):
                key = "mrs" if outcome.kind == "merge_request" else "issues"
                rows[path or NO_PROJECT][key].add(outcome.gitlab_id)
        for _, day, path, metric, value in facts.activity:
            if window.contains(day) and in_scope(path, scopes) and metric in ACTIVITY_TOTALS:
                rows[path or NO_PROJECT]["events"] += value
        total = sum(r["seconds"] for r in rows.values())
        result = [
            {
                "path": path,
                "seconds": row["seconds"],
                "share": round(row["seconds"] / total, 4) if total else 0.0,
                "mrs_merged": len(row["mrs"]),
                "issues_closed": len(row["issues"]),
                "review_events": row["events"],
            }
            for path, row in rows.items()
        ]
        result.sort(key=lambda r: (-r["seconds"], -r["mrs_merged"] - r["issues_closed"], r["path"]))
        return result

    def _spread(
        self, facts: _Facts, users: dict[int, User], scopes: list[str], start: date
    ) -> list[dict[str, Any]]:
        """Knowledge spread: who touched a project's merge requests in the last weeks."""
        window = Window(start, self.today + timedelta(days=1))
        people: dict[str, set[int]] = defaultdict(set)
        for uid, outcome, day, path in facts.outcomes:
            if outcome.kind == "merge_request" and path and window.contains(day):
                people[path].add(uid)
        for uid, day, path, metric, value in facts.activity:
            if metric in SPREAD_METRICS and value and path and window.contains(day):
                people[path].add(uid)
        rows = [
            {
                "path": path,
                "people": sorted(users[uid].name for uid in uids if uid in users),
                "count": len(uids),
                "single": len(uids) == 1,
            }
            for path, uids in people.items()
            if in_scope(path, scopes)
        ]
        rows.sort(key=lambda r: (r["count"], r["path"]))
        return rows

    @staticmethod
    def _people(
        facts: _Facts, users: dict[int, User], window: Window, scopes: list[str]
    ) -> list[dict[str, Any]]:
        rows: dict[int, dict[str, Any]] = {
            uid: {
                "seconds": 0,
                "days": set(),
                "mrs_merged": set(),
                "issues_closed": set(),
                "approvals": 0,
                "review_comments": 0,
            }
            for uid in users
        }
        for uid, day, path, seconds in facts.timelogs:
            if uid in rows and window.contains(day) and in_scope(path, scopes):
                rows[uid]["seconds"] += seconds
                rows[uid]["days"].add(day)
        for uid, outcome, day, path in facts.outcomes:
            if uid in rows and window.contains(day) and in_scope(path, scopes):
                key = "mrs_merged" if outcome.kind == "merge_request" else "issues_closed"
                rows[uid][key].add(outcome.gitlab_id)
        for uid, day, path, metric, value in facts.activity:
            if uid in rows and window.contains(day) and in_scope(path, scopes):
                if metric == "mr_approved":
                    rows[uid]["approvals"] += value
                elif metric == "mr_commented":
                    rows[uid]["review_comments"] += value
        result = [
            {
                "id": uid,
                "name": users[uid].name,
                "username": users[uid].username,
                "seconds": row["seconds"],
                "days_with_time": len(row["days"]),
                "mrs_merged": len(row["mrs_merged"]),
                "issues_closed": len(row["issues_closed"]),
                "approvals": row["approvals"],
                "review_comments": row["review_comments"],
            }
            for uid, row in rows.items()
        ]
        result.sort(key=lambda r: (r["name"].lower(), r["username"]))
        return result

    def _history(
        self, member_ids: list[int], users: dict[int, User], history_from: date
    ) -> dict[str, Any]:
        """Whether the history of every member has been loaded at least once."""
        pending = []
        last_success: list[datetime] = []
        for uid in member_ids:
            states = [store.find_state(self.session, c, uid) for c in HISTORY_CATEGORIES]
            if any(s is None or s.last_success_at is None for s in states):
                pending.append(users[uid].name if uid in users else str(uid))
            else:
                last_success.extend(s.last_success_at for s in states if s and s.last_success_at)
        return {
            "from": history_from,
            "complete": not pending,
            "pending": sorted(pending),
            "updated_at": min(last_success) if last_success and not pending else None,
        }


def _quartiles(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    if len(values) == 1:
        return {
            "median": round(values[0], 1),
            "p25": round(values[0], 1),
            "p75": round(values[0], 1),
            "count": 1,
        }
    q1, q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return {"median": round(q2, 1), "p25": round(q1, 1), "p75": round(q3, 1), "count": len(values)}


# ---------------------------------------------------------------------- digest


def _duration(seconds: int) -> str:
    hours, rest = divmod(round(seconds / 60), 60)
    return f"{hours}h {rest:02d}m" if hours else f"{rest}m"


def digest(
    session: Session, settings: Settings, now: datetime, team_id: int
) -> dict[str, Any] | None:
    """Markdown summary of the last completed calendar week, ready to paste into a report."""
    reporter = TeamReporter(session, settings, now)
    report = reporter.report(team_id, "week", 1)
    if report is None:
        return None
    start: date = report["period"]["start"]
    end = start + timedelta(days=7)
    member_ids = store.team_members(session, [team_id]).get(team_id, [])
    scopes = report["team"]["scopes"]
    facts = reporter._facts(member_ids, start, end)
    window = Window(start, end)
    names = (
        {u.id: u.name for u in session.scalars(select(User).where(User.id.in_(member_ids)))}
        if member_ids
        else {}
    )
    totals = report["totals"]
    lines = [
        f"## {report['team']['name']} — {report['period']['label']}",
        "",
        f"- Logged time: **{_duration(totals['seconds'])}**",
        f"- Merge requests merged: **{totals['mrs_merged']}** · opened: {totals['mrs_opened']}",
        f"- Issues closed: **{totals['issues_closed']}**",
        f"- Reviews: {totals['approvals']} approvals, {totals['review_comments']} comments",
    ]
    if scopes:
        lines.append(f"- Scope: {', '.join(scopes)}")
    if report["projects"]:
        lines += ["", "### Time per project", ""]
        lines += [
            f"- {p['path']}: {_duration(p['seconds'])}" for p in report["projects"] if p["seconds"]
        ]
    seen: set[tuple[str, int]] = set()
    finished: dict[str, list[str]] = {"merge_request": [], "issue": []}
    for uid, outcome, day, path in sorted(facts.outcomes, key=lambda f: f[1].finished_at):
        key = (outcome.kind, outcome.gitlab_id)
        if key in seen or not window.contains(day) or not in_scope(path, scopes):
            continue
        seen.add(key)
        link = safe_url(outcome.web_url, settings)
        ref = f"[{outcome.reference}]({link})" if link else outcome.reference
        finished[outcome.kind].append(f"- {ref} {outcome.title} ({names.get(uid, uid)})")
    if finished["merge_request"]:
        lines += ["", "### Merged", "", *finished["merge_request"]]
    if finished["issue"]:
        lines += ["", "### Closed issues", "", *finished["issue"]]
    if not report["history"]["complete"]:
        lines += [
            "",
            "_History is still loading for: " + ", ".join(report["history"]["pending"]) + "_",
        ]
    return {"period": report["period"], "markdown": "\n".join(lines) + "\n"}

"""Meeting summary: the real outcome of each person's work in a period, from the local cache.

Built on demand (a button in the browser), never on polling and never with a language model:
plain rules over GitLab facts. The headline is the first non-empty level of

1. delivered (issues closed, MRs merged), 2. unblocked others (reviewed MRs that got merged),
3. in progress, 4. discussion and triage, 5. the last delivery within ``LOOKBACK_DAYS``,
6. nothing recorded in GitLab.

Delivered items are ordered by an item weight built from facts (priority label, incident or bug,
milestone, due date, size, age). The weight orders *items*; it is never shown and never compares
people. Logged hours are context at the end, not the result.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from gitlab_team_pulse import store
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.dashboard import DashboardReader, safe_url, user_freshness, user_payload
from gitlab_team_pulse.models import (
    ActivityDaily,
    Milestone,
    Outcome,
    Project,
    SyncState,
    TimelogDaily,
    TimelogRecord,
    User,
    WorkItemAssignee,
    WorkItemRecord,
)
from gitlab_team_pulse.sync import RuntimeState

Level = Literal["delivered", "unblocked", "in_progress", "discussion", "last_delivery", "none"]
PERIOD_DAYS = (2, 7, 14, 30)
MAX_DAYS = 31
MAX_PEOPLE = 50
LOOKBACK_DAYS = 90
DELIVERED_SHOWN = 5
LINES_SHOWN = 3
HEADLINE_ITEMS = 3
TITLE_CHARS = 70

TOP_PRIORITY = {"1", "p1", "high", "highest", "critical", "urgent", "blocker"}
SECOND_PRIORITY = {"2", "p2", "medium"}
WEIGHTS = {
    "base": 10,
    "priority_top": 8,
    "priority_second": 4,
    "incident": 8,
    "security": 6,
    "bug": 4,
    "customer": 3,
    "feature": 3,
    "milestone": 3,
    "milestone_due": 2,
    "on_time": 3,
    "overdue_done": 2,
    "size_large": 4,
    "size_medium": 2,
    "age_long": 4,
    "age_medium": 2,
    "linked": 2,
}
LARGE_SECONDS = 16 * 3600
MEDIUM_SECONDS = 4 * 3600
LONG_AGE_DAYS = 90
MEDIUM_AGE_DAYS = 30
MILESTONE_DUE_DAYS = 14

RULES = [
    (
        "Delivered",
        "Issues closed and merge requests merged in the period, assigned to (issues, MRs) or "
        "authored by (MRs) the person. Each item once; an MR whose title names an issue of the "
        "same project (#12, or \"Resolve 'title'\") is shown with that issue.",
    ),
    (
        "Order",
        "Delivered items come first when they carry a top priority label (Prio/priority 1, "
        "high, critical), are an incident, security or bug fix, a customer item or a feature, "
        "belong to a milestone (more when it is due within two weeks), were finished on or "
        "before their due date (or finished although overdue), are large (16 h or more spent or "
        "estimated) or were open for a long time (30 or 90 days). The order is about items, "
        "never about people.",
    ),
    (
        "Unblocked others",
        "Merge requests of colleagues where the person is a reviewer and that were merged in "
        "the period; approvals and review comments come from the person's GitLab events.",
    ),
    (
        "In progress",
        "Open assigned items updated or worked on (time logged) in the period, MRs opened, "
        "in review or in draft.",
    ),
    (
        "Needs a decision",
        "Open items of the person that are overdue, stalled, cannot be merged as they are, have "
        "no reviewer or wait for this person's review (the same rules as Needs attention).",
    ),
    (
        "Fallback",
        "Without delivery the headline falls back to unblocked others, then work in progress, "
        "then discussion (issues opened, comments), then the last delivery within "
        f"{LOOKBACK_DAYS} days. If GitLab has nothing, the summary says so: absence or work "
        "outside GitLab are possible.",
    ),
    (
        "Hours",
        "Time logged in GitLab is context at the end, not the result.",
    ),
]


@dataclass
class _Item:
    record: WorkItemRecord | None
    kind: str
    gitlab_id: int
    project_id: int | None
    iid: int
    reference: str
    title: str
    web_url: str | None
    finished_at: datetime
    created_at: datetime | None
    time_estimate: int
    time_spent: int
    weight: int = 0
    reasons: list[str] = field(default_factory=list)
    via: list[str] = field(default_factory=list)


def _short(title: str) -> str:
    title = " ".join(title.split())
    return title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1].rstrip() + "…"


def _duration(seconds: int) -> str:
    hours, rest = divmod(round(seconds / 60), 60)
    return f"{hours}h {rest:02d}m" if hours else f"{rest}m"


def _label_words(labels: list[str]) -> set[str]:
    words: set[str] = set()
    for label in labels:
        for part in label.lower().split("::"):
            words.update(re.split(r"[\s/_-]+", part.strip()))
    return words


def _age_days(start: datetime | None, end: datetime) -> int:
    return (end - start).days if start is not None and end >= start else 0


_RESOLVE = re.compile(r"""^(?:draft:\s*)?(?:resolve|closes?|fix(?:es)?)\s+["'](.+)["']\s*$""", re.I)
_ISSUE_REF = re.compile(r"(?<![\w!&])#(\d+)\b")


def _normalize(title: str) -> str:
    title = re.sub(r"^\s*(?:draft:|wip:)\s*", "", title, flags=re.I)
    match = _RESOLVE.match(title.strip())
    return " ".join((match.group(1) if match else title).lower().split())


class MeetingSummary:
    def __init__(
        self, session: Session, settings: Settings, runtime: RuntimeState, now: datetime
    ) -> None:
        self.session = session
        self.settings = settings
        self.runtime = runtime
        self.now = now
        self.tz = settings.tz
        self.today = now.astimezone(self.tz).date()
        self.names: dict[str, str] = {}

    def _midnight(self, day: date) -> datetime:
        return datetime.combine(day, datetime.min.time(), self.tz).astimezone(UTC)

    def build(self, users: list[User], days: int) -> dict[str, Any]:
        start_day = self.today - timedelta(days=days - 1)
        start = self._midnight(start_day)
        ids = [u.id for u in users]
        records = self._records(ids)
        outcomes = self._outcomes(ids, self.now - timedelta(days=LOOKBACK_DAYS))
        activity = self._activity(ids, start_day)
        hours = self._hours(ids, start_day, start)
        item_seconds = self._item_seconds(ids, start)
        milestones = self._milestones(records)
        paths = self._paths(records, outcomes)
        self.names = self._names(records)
        reader = DashboardReader(self.session, self.settings, self.runtime, self.now)
        attention = reader.attention(users, reader.work(ids)) if ids else {"items": []}
        states = {
            s.key: s
            for s in self.session.scalars(select(SyncState).where(SyncState.user_id.in_(ids)))
        }
        people = []
        for user in users:
            people.append(
                self._person(
                    user,
                    start,
                    records=records.get(user.id, []),
                    outcomes=outcomes.get(user.id, []),
                    activity=activity.get(user.id, {}),
                    hours=hours.get(user.id, 0),
                    item_seconds=item_seconds.get(user.id, {}),
                    milestones=milestones,
                    paths=paths,
                    attention=[
                        row
                        for row in attention["items"]
                        if any(p["username"] == user.username for p in row["people"])
                    ],
                    freshness=user_freshness(
                        states, user.id, self.runtime, self.settings, self.now
                    ),
                )
            )
        period = {
            "days": days,
            "start": start_day,
            "end": self.today,
            "label": f"{start_day:%d %b} to {self.today:%d %b %Y}",
        }
        result = {
            "generated_at": self.now,
            "timezone": self.settings.timezone,
            "period": period,
            "people": people,
            "rules": [{"name": name, "rule": rule} for name, rule in RULES],
        }
        result["markdown"] = markdown(result)
        return result

    # ------------------------------------------------------------------ queries

    def _records(self, ids: list[int]) -> dict[int, list[tuple[str, WorkItemRecord]]]:
        if not ids:
            return {}
        rows = self.session.execute(
            select(WorkItemAssignee.user_id, WorkItemAssignee.relation, WorkItemRecord)
            .join(WorkItemRecord, WorkItemRecord.id == WorkItemAssignee.work_item_id)
            .where(WorkItemAssignee.user_id.in_(ids))
        ).all()
        result: dict[int, list[tuple[str, WorkItemRecord]]] = defaultdict(list)
        for user_id, relation, record in rows:
            result[user_id].append((relation, record))
        return result

    def _outcomes(self, ids: list[int], since: datetime) -> dict[int, list[Outcome]]:
        if not ids:
            return {}
        rows = self.session.scalars(
            select(Outcome).where(Outcome.user_id.in_(ids), Outcome.finished_at >= since)
        )
        result: dict[int, list[Outcome]] = defaultdict(list)
        for row in rows:
            result[row.user_id].append(row)
        return result

    def _activity(self, ids: list[int], start: date) -> dict[int, dict[str, int]]:
        if not ids:
            return {}
        rows = self.session.execute(
            select(ActivityDaily.user_id, ActivityDaily.metric, ActivityDaily.value).where(
                ActivityDaily.user_id.in_(ids), ActivityDaily.day >= start
            )
        ).all()
        result: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for user_id, metric, value in rows:
            result[user_id][metric] += value
        return result

    def _hours(self, ids: list[int], start_day: date, start: datetime) -> dict[int, int]:
        """Seconds logged in the period: the daily history, else the short raw cache."""
        if not ids:
            return {}
        history: dict[int, int] = defaultdict(int)
        for user_id, seconds in self.session.execute(
            select(TimelogDaily.user_id, TimelogDaily.seconds).where(
                TimelogDaily.user_id.in_(ids), TimelogDaily.day >= start_day
            )
        ):
            history[user_id] += seconds
        raw: dict[int, int] = defaultdict(int)
        for user_id, seconds in self.session.execute(
            select(TimelogRecord.user_id, TimelogRecord.seconds).where(
                TimelogRecord.user_id.in_(ids), TimelogRecord.spent_at >= start
            )
        ):
            raw[user_id] += seconds
        return {uid: max(history.get(uid, 0), raw.get(uid, 0)) for uid in ids}

    def _item_seconds(
        self, ids: list[int], start: datetime
    ) -> dict[int, dict[tuple[int | None, str | None, int | None], int]]:
        """Seconds logged per (project, kind, iid) in the period, from the raw time entries."""
        if not ids:
            return {}
        result: dict[int, dict[tuple[int | None, str | None, int | None], int]] = defaultdict(
            lambda: defaultdict(int)
        )
        for log in self.session.scalars(
            select(TimelogRecord).where(
                TimelogRecord.user_id.in_(ids), TimelogRecord.spent_at >= start
            )
        ):
            result[log.user_id][(log.project_id, log.target_kind, log.target_iid)] += log.seconds
        return result

    def _milestones(
        self, records: dict[int, list[tuple[str, WorkItemRecord]]]
    ) -> dict[int, Milestone]:
        ids = {r.milestone_id for rows in records.values() for _, r in rows if r.milestone_id}
        if not ids:
            return {}
        return {
            m.id: m for m in self.session.scalars(select(Milestone).where(Milestone.id.in_(ids)))
        }

    def _names(self, records: dict[int, list[tuple[str, WorkItemRecord]]]) -> dict[str, str]:
        authors = {r.author for rows in records.values() for rel, r in rows if rel == "reviewer"}
        authors.discard(None)
        if not authors:
            return {}
        rows = self.session.execute(
            select(User.username, User.name).where(User.username.in_(authors))
        )
        return {row.username: row.name for row in rows}

    def _paths(
        self,
        records: dict[int, list[tuple[str, WorkItemRecord]]],
        outcomes: dict[int, list[Outcome]],
    ) -> dict[int, str]:
        ids = {r.project_id for rows in records.values() for _, r in rows if r.project_id}
        ids |= {o.project_id for rows in outcomes.values() for o in rows if o.project_id}
        if not ids:
            return {}
        return {
            p.id: p.path_with_namespace
            for p in self.session.scalars(select(Project).where(Project.id.in_(ids)))
        }

    # ------------------------------------------------------------------ one person

    def _delivered(
        self,
        start: datetime,
        records: list[tuple[str, WorkItemRecord]],
        outcomes: list[Outcome],
    ) -> list[_Item]:
        found: dict[tuple[str, int], _Item] = {}
        by_key = {(r.kind, r.gitlab_id): r for _, r in records}
        for relation, record in records:
            finished = _finished(record)
            if relation != "assignee" or finished is None or finished < start:
                continue
            found[(record.kind, record.gitlab_id)] = _item_from_record(record, finished)
        for outcome in outcomes:
            key = (outcome.kind, outcome.gitlab_id)
            if outcome.finished_at < start or key in found:
                continue
            known = by_key.get(key)
            found[key] = (
                _item_from_record(known, outcome.finished_at)
                if known is not None
                else _Item(
                    record=None,
                    kind=outcome.kind,
                    gitlab_id=outcome.gitlab_id,
                    project_id=outcome.project_id,
                    iid=outcome.iid,
                    reference=outcome.reference,
                    title=outcome.title,
                    web_url=outcome.web_url,
                    finished_at=outcome.finished_at,
                    created_at=outcome.created_at,
                    time_estimate=outcome.time_estimate,
                    time_spent=outcome.time_spent,
                )
            )
        return _fold_merge_requests(list(found.values()))

    def _weigh(self, item: _Item, milestones: dict[int, Milestone]) -> None:
        weight = WEIGHTS["base"]
        reasons: list[str] = []
        record = item.record
        labels = list(record.labels) if record is not None else []
        words = _label_words(labels)
        priority = (record.priority or "").strip() if record is not None else ""
        if priority.lower() in TOP_PRIORITY:
            weight += WEIGHTS["priority_top"]
            reasons.append(f"Prio {priority}")
        elif priority.lower() in SECOND_PRIORITY:
            weight += WEIGHTS["priority_second"]
            reasons.append(f"Prio {priority}")
        if record is not None and record.issue_type == "incident":
            weight += WEIGHTS["incident"]
            reasons.append("incident")
        if "security" in words:
            weight += WEIGHTS["security"]
            reasons.append("security")
        if words & {"bug", "defect", "regression"} and "incident" not in reasons:
            weight += WEIGHTS["bug"]
            reasons.append("bug fix")
        if words & {"customer", "customers"}:
            weight += WEIGHTS["customer"]
            reasons.append("customer")
        if words & {"feature", "enhancement", "story"}:
            weight += WEIGHTS["feature"]
            reasons.append("feature")
        if record is not None and record.milestone and not _is_backlog(record.milestone):
            weight += WEIGHTS["milestone"]
            milestone = milestones.get(record.milestone_id or 0)
            due = milestone.due_date if milestone is not None else None
            if due is not None and (due - self.today).days <= MILESTONE_DUE_DAYS:
                weight += WEIGHTS["milestone_due"]
            reasons.append(f"milestone {record.milestone}")
        due_date = record.due_date if record is not None else None
        if due_date is not None:
            finished_day = item.finished_at.astimezone(self.tz).date()
            if finished_day <= due_date:
                weight += WEIGHTS["on_time"]
                reasons.append("on time")
            else:
                weight += WEIGHTS["overdue_done"]
                late = (finished_day - due_date).days
                reasons.append(f"done {late} d after due date")
        effort = max(item.time_spent, item.time_estimate)
        if effort >= LARGE_SECONDS:
            weight += WEIGHTS["size_large"]
            reasons.append(f"large: {_duration(effort)}")
        elif effort >= MEDIUM_SECONDS:
            weight += WEIGHTS["size_medium"]
            reasons.append(f"{_duration(effort)}")
        age = _age_days(item.created_at, item.finished_at)
        if age >= LONG_AGE_DAYS:
            weight += WEIGHTS["age_long"]
            reasons.append(f"open {age} d")
        elif age >= MEDIUM_AGE_DAYS:
            weight += WEIGHTS["age_medium"]
            reasons.append(f"open {age} d")
        if item.via:
            weight += WEIGHTS["linked"]
        item.weight = weight
        item.reasons = reasons

    def _person(
        self,
        user: User,
        start: datetime,
        *,
        records: list[tuple[str, WorkItemRecord]],
        outcomes: list[Outcome],
        activity: dict[str, int],
        hours: int,
        item_seconds: dict[tuple[int | None, str | None, int | None], int],
        milestones: dict[int, Milestone],
        paths: dict[int, str],
        attention: list[dict[str, Any]],
        freshness: dict[str, Any],
    ) -> dict[str, Any]:
        delivered = self._delivered(start, records, outcomes)
        for item in delivered:
            self._weigh(item, milestones)
        delivered.sort(key=lambda i: (-i.weight, -i.finished_at.timestamp(), i.reference))
        unblocked = self._unblocked(user, start, records)
        progress = self._progress(start, records, item_seconds, milestones)
        risks = _risks(attention)
        discussion = {
            "issues_opened": activity.get("issue_opened", 0),
            "comments": activity.get("issue_commented", 0) + activity.get("mr_commented", 0),
        }
        approvals = activity.get("mr_approved", 0)
        review_comments = activity.get("mr_commented", 0)
        last = None
        if delivered:
            level: Level = "delivered"
        elif unblocked or approvals:
            level = "unblocked"
        elif progress:
            level = "in_progress"
        elif discussion["issues_opened"] or discussion["comments"]:
            level = "discussion"
        else:
            last = self._last_delivery(start, records, outcomes)
            level = "last_delivery" if last else "none"
        payload_items = [self._item_payload(i, paths) for i in delivered]
        person = {
            "user": user_payload(user, self.settings),
            "freshness": freshness,
            "level": level,
            "headline": "",
            "delivered": payload_items[:DELIVERED_SHOWN],
            "delivered_total": len(delivered),
            "unblocked": {
                "merged": unblocked[:LINES_SHOWN],
                "merged_total": len(unblocked),
                "approvals": approvals,
                "review_comments": review_comments,
            },
            "progress": progress[:LINES_SHOWN],
            "progress_total": len(progress),
            "risks": risks[:LINES_SHOWN],
            "risks_total": len(risks),
            "discussion": discussion,
            "last_delivery": last,
            "logged_seconds": hours,
        }
        person["headline"] = headline(person)
        return person

    def _item_payload(self, item: _Item, paths: dict[int, str]) -> dict[str, Any]:
        return {
            "kind": item.kind,
            "reference": item.reference,
            "title": item.title,
            "web_url": safe_url(item.web_url, self.settings),
            "project": paths.get(item.project_id) if item.project_id else None,
            "finished_at": item.finished_at,
            "reasons": item.reasons,
            "via": item.via,
        }

    def _unblocked(
        self, user: User, start: datetime, records: list[tuple[str, WorkItemRecord]]
    ) -> list[dict[str, Any]]:
        rows = []
        for relation, record in records:
            if (
                relation == "reviewer"
                and record.kind == "merge_request"
                and record.merged_at is not None
                and record.merged_at >= start
                and record.author != user.username
            ):
                author = record.author or ""
                rows.append(
                    {
                        "reference": record.reference,
                        "title": record.title,
                        "web_url": safe_url(record.web_url, self.settings),
                        "author": self.names.get(author, author),
                        "finished_at": record.merged_at,
                    }
                )
        rows.sort(key=lambda r: (r["finished_at"], r["reference"]), reverse=True)
        return rows

    def _progress(
        self,
        start: datetime,
        records: list[tuple[str, WorkItemRecord]],
        item_seconds: dict[tuple[int | None, str | None, int | None], int],
        milestones: dict[int, Milestone],
    ) -> list[dict[str, Any]]:
        rows: dict[int, dict[str, Any]] = {}
        for relation, record in records:
            if relation != "assignee" or record.state not in store.OPEN_STATES:
                continue
            seconds = item_seconds.get((record.project_id, record.kind, record.iid), 0)
            created = record.created_at is not None and record.created_at >= start
            if record.updated_at < start and not seconds:
                continue
            if record.kind == "merge_request":
                status = (
                    "draft"
                    if record.draft
                    else ("in review" if record.reviewers else "open, no reviewer")
                )
                if created:
                    status = f"opened, {status}"
            else:
                status = "opened" if created else "worked on"
            item = _item_from_record(record, record.updated_at)
            self._weigh(item, milestones)
            rows[record.id] = {
                "kind": record.kind,
                "reference": record.reference,
                "title": record.title,
                "web_url": safe_url(record.web_url, self.settings),
                "status": status,
                "logged_seconds": seconds,
                "reasons": [r for r in item.reasons if not r.startswith(("on time", "done "))],
                "_order": (-item.weight, -seconds, -record.updated_at.timestamp()),
            }
        ordered = sorted(rows.values(), key=lambda r: r["_order"])
        for row in ordered:
            del row["_order"]
        return ordered

    def _last_delivery(
        self,
        start: datetime,
        records: list[tuple[str, WorkItemRecord]],
        outcomes: list[Outcome],
    ) -> dict[str, Any] | None:
        candidates: list[tuple[datetime, str, str, str | None]] = [
            (o.finished_at, o.reference, o.title, o.web_url)
            for o in outcomes
            if o.finished_at < start
        ]
        for relation, record in records:
            finished = _finished(record)
            if relation == "assignee" and finished is not None and finished < start:
                candidates.append((finished, record.reference, record.title, record.web_url))
        floor = self.now - timedelta(days=LOOKBACK_DAYS)
        candidates = [c for c in candidates if c[0] >= floor]
        if not candidates:
            return None
        finished, reference, title, url = max(candidates, key=lambda c: (c[0], c[1]))
        return {
            "reference": reference,
            "title": title,
            "web_url": safe_url(url, self.settings),
            "finished_at": finished,
            "days_ago": (self.today - finished.astimezone(self.tz).date()).days,
        }


def _is_backlog(title: str) -> bool:
    """Backlog-style milestones ("Backlog", "Icebox", "Someday") are not a plan."""
    return bool(_label_words([title]) & {"backlog", "icebox", "someday", "later"})


def short_reference(reference: str) -> str:
    """``group/project#12`` → ``#12``; ``group&3`` → ``&3``."""
    match = re.search(r"[#!&]\d+$", reference)
    return match.group(0) if match else reference


def _finished(record: WorkItemRecord) -> datetime | None:
    if record.kind == "merge_request":
        return record.merged_at if record.state == "merged" else None
    return record.closed_at if record.state == "closed" else None


def _item_from_record(record: WorkItemRecord, finished: datetime) -> _Item:
    return _Item(
        record=record,
        kind=record.kind,
        gitlab_id=record.gitlab_id,
        project_id=record.project_id,
        iid=record.iid,
        reference=record.reference,
        title=record.title,
        web_url=record.web_url,
        finished_at=finished,
        created_at=record.created_at,
        time_estimate=record.time_estimate,
        time_spent=record.time_spent,
    )


def _fold_merge_requests(items: list[_Item]) -> list[_Item]:
    """Show an MR that delivers an issue of the same project as part of that issue."""
    issues = [i for i in items if i.kind == "issue"]
    by_iid = {(i.project_id, i.iid): i for i in issues}
    by_title = {(i.project_id, _normalize(i.title)): i for i in issues}
    result = []
    for item in items:
        if item.kind != "merge_request":
            result.append(item)
            continue
        target = by_title.get((item.project_id, _normalize(item.title)))
        if target is None:
            for number in _ISSUE_REF.findall(item.title):
                target = by_iid.get((item.project_id, int(number)))
                if target is not None:
                    break
        if target is None:
            result.append(item)
            continue
        target.via.append(item.reference)
        target.time_spent = max(target.time_spent, item.time_spent)
        target.time_estimate = max(target.time_estimate, item.time_estimate)
    return result


def _risks(attention: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Warnings and errors of the attention list for this person, worst first."""
    rows = []
    for row in attention:
        reasons = [r for r in row["reasons"] if r["severity"] != "info"]
        if not reasons:
            continue
        rows.append(
            {
                "reference": row["reference"],
                "title": row["title"],
                "web_url": row["web_url"],
                "severity": reasons[0]["severity"],
                "reasons": [_reason_text(r) for r in reasons],
            }
        )
    return rows


def _reason_text(reason: dict[str, Any]) -> str:
    code = reason["code"]
    days = reason.get("days")
    if code == "stalled":
        return f"no update for {days} d"
    if code == "overdue":
        return f"overdue by {days} d"
    if code == "due_soon":
        return "due today" if days == 0 else f"due in {days} d"
    if code == "review":
        return f"waits for this review for {days} d"
    if code == "cannot_merge":
        return "cannot merge: " + ", ".join(p.replace("_", " ") for p in reason["problems"])
    if code == "no_reviewer":
        return "no reviewer"
    return str(code).replace("_", " ")


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def headline(person: dict[str, Any]) -> str:
    """One crisp sentence: the highest level that has facts."""
    level = person["level"]
    unblocked = person["unblocked"]
    if level == "delivered":
        top = person["delivered"][:HEADLINE_ITEMS]
        parts = [
            f"{short_reference(i['reference'])} {_short(i['title'])}"
            + (f" ({i['reasons'][0]})" if i["reasons"] else "")
            for i in top
        ]
        more = person["delivered_total"] - len(top)
        text = f"Delivered {person['delivered_total']}: " + "; ".join(parts)
        if more > 0:
            text += f"; and {more} more"
        if unblocked["merged_total"]:
            text += f". Also unblocked {_plural(unblocked['merged_total'], 'MR')} of others"
        return text + "."
    if level == "unblocked":
        bits = []
        if unblocked["merged_total"]:
            bits.append(f"reviewed {_plural(unblocked['merged_total'], 'MR')} that got merged")
        if unblocked["approvals"]:
            bits.append(_plural(unblocked["approvals"], "approval"))
        return "No own delivery in GitLab; unblocked others: " + ", ".join(bits) + "."
    if level == "in_progress":
        top = person["progress"][:HEADLINE_ITEMS]
        return (
            "In progress: "
            + "; ".join(
                f"{short_reference(i['reference'])} {_short(i['title'])} ({i['status']})"
                for i in top
            )
            + "."
        )
    if level == "discussion":
        found = person["discussion"]
        bits = []
        if found["issues_opened"]:
            bits.append(_plural(found["issues_opened"], "issue") + " opened")
        if found["comments"]:
            bits.append(_plural(found["comments"], "comment"))
        return "Discussion and triage: " + ", ".join(bits) + "."
    if level == "last_delivery":
        last = person["last_delivery"]
        return (
            "Nothing delivered in GitLab in this period; last delivery: "
            f"{short_reference(last['reference'])} "
            f"{_short(last['title'])}, {_plural(last['days_ago'], 'day')} ago."
        )
    return (
        "No GitLab activity recorded in this period (absence or work outside GitLab are possible)."
    )


def _md_link(reference: str, url: str | None) -> str:
    return f"[{reference}]({url})" if url else reference


def markdown(summary: dict[str, Any]) -> str:
    lines = [f"## Meeting summary · {summary['period']['label']}", ""]
    for person in summary["people"]:
        lines += [f"### {person['user']['name']}", "", f"**{person['headline']}**", ""]
        for item in person["delivered"]:
            via = f" (with {', '.join(item['via'])})" if item["via"] else ""
            why = f" · {', '.join(item['reasons'])}" if item["reasons"] else ""
            lines.append(
                f"- ✅ {_md_link(item['reference'], item['web_url'])} {item['title']}{via}{why}"
            )
        if (hidden := person["delivered_total"] - len(person["delivered"])) > 0:
            lines.append(f"- … and {hidden} more delivered")
        unblocked = person["unblocked"]
        for row in unblocked["merged"]:
            lines.append(
                f"- 🤝 Reviewed {_md_link(row['reference'], row['web_url'])} {row['title']}"
                + (f" by {row['author']}" if row["author"] else "")
                + ", merged"
            )
        if person["level"] != "in_progress":
            for row in person["progress"]:
                lines.append(
                    f"- ⏳ {_md_link(row['reference'], row['web_url'])} {row['title']} "
                    f"({row['status']})"
                )
        for row in person["risks"]:
            lines.append(
                f"- ⚠️ {_md_link(row['reference'], row['web_url'])} {row['title']}: "
                + ", ".join(row["reasons"])
            )
        context = []
        if unblocked["approvals"]:
            context.append(_plural(unblocked["approvals"], "approval"))
        if unblocked["review_comments"]:
            context.append(_plural(unblocked["review_comments"], "review comment"))
        if person["logged_seconds"]:
            context.append(f"{_duration(person['logged_seconds'])} logged")
        if context:
            lines.append(f"- Context: {'; '.join(context)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

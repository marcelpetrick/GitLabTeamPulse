"""Normalized GitLab records and the functions that build them from raw API payloads.

Nothing outside the ``gitlab`` package touches raw JSON. Every ``parse_*`` function raises
``GitLabResponseError`` when a payload violates the API contract.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from gitlab_team_pulse.gitlab.errors import GitLabResponseError

WorkKind = Literal["issue", "merge_request", "epic"]
Relation = Literal["assignee", "reviewer"]
ActivityCategory = Literal["push", "comment", "issue", "merge_request", "other"]

BOT_USER_TYPES = {"project_bot", "ghost"}
SERVICE_USER_TYPES = {"service_account", "service_user", "import_user", "placeholder"}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class GitLabUser(_Frozen):
    id: int
    username: str
    name: str
    avatar_url: str | None = None
    web_url: str | None = None
    created_at: datetime | None = None
    state: str = "unknown"
    account_type: str = "unknown"
    is_admin: bool | None = None


class GitLabProject(_Frozen):
    id: int
    name: str
    path_with_namespace: str
    web_url: str | None = None


class WorkItem(_Frozen):
    kind: WorkKind
    gitlab_id: int
    iid: int
    project_id: int | None
    reference: str
    title: str
    state: str
    issue_type: str | None = None
    labels: tuple[str, ...] = ()
    milestone: str | None = None
    priority: str | None = None
    due_date: date | None = None
    created_at: datetime | None = None
    updated_at: datetime
    closed_at: datetime | None = None
    web_url: str | None = None
    author: str | None = None
    assignees: tuple[str, ...] = ()
    draft: bool = False
    relation: Relation = "assignee"


class ActivityEvent(_Frozen):
    id: int
    project_id: int | None
    action_name: str
    target_type: str | None
    target_title: str | None
    category: ActivityCategory
    summary: str
    created_at: datetime
    url_path: str | None
    """Path relative to the project web URL (e.g. ``/-/issues/12``); None = project page."""


class Timelog(_Frozen):
    id: int
    spent_at: datetime
    seconds: int
    summary: str | None
    project_id: int | None
    project_path: str | None
    target_kind: WorkKind | None
    target_iid: int | None
    target_title: str | None
    web_url: str | None


def guarded[T](what: str, parser: Callable[[dict[str, Any]], T], payload: object) -> T:
    """Run ``parser`` and convert contract violations into ``GitLabResponseError``."""
    if not isinstance(payload, dict):
        raise GitLabResponseError(f"unexpected {what} payload: expected an object")
    try:
        return parser(payload)
    except GitLabResponseError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise GitLabResponseError(f"unexpected {what} payload: {type(exc).__name__} {exc}") from exc


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromisoformat(str(value))


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(str(value)[:10])


def classify_account(payload: dict[str, Any]) -> str:
    """Return human/bot/service/unknown; only authoritative metadata is trusted."""
    user_type = payload.get("user_type")
    if isinstance(user_type, str) and user_type:
        if user_type == "human":
            return "human"
        if user_type in SERVICE_USER_TYPES:
            return "service"
        if user_type in BOT_USER_TYPES or user_type.endswith("_bot"):
            return "bot"
        return "unknown"
    bot = payload.get("bot")
    if bot is True:
        return "bot"
    if bot is False:
        return "human"
    return "unknown"


def parse_user(payload: dict[str, Any]) -> GitLabUser:
    return GitLabUser(
        id=int(payload["id"]),
        username=str(payload["username"]),
        name=str(payload.get("name") or payload["username"]),
        avatar_url=payload.get("avatar_url") or None,
        web_url=payload.get("web_url") or None,
        created_at=_dt(payload.get("created_at")),
        state=str(payload.get("state") or "unknown").lower(),
        account_type=classify_account(payload),
        is_admin=payload.get("is_admin") if isinstance(payload.get("is_admin"), bool) else None,
    )


def parse_project(payload: dict[str, Any]) -> GitLabProject:
    return GitLabProject(
        id=int(payload["id"]),
        name=str(payload.get("name") or payload["path_with_namespace"]),
        path_with_namespace=str(payload["path_with_namespace"]),
        web_url=payload.get("web_url") or None,
    )


def priority_from_labels(labels: tuple[str, ...]) -> str | None:
    """Priority from scoped labels such as ``priority::high`` or ``Prio::1``."""
    for label in labels:
        scope, sep, value = label.partition("::")
        if sep and scope.strip().lower() in {"priority", "prio"} and value.strip():
            return value.strip()
    return None


def parse_work_item(payload: dict[str, Any], kind: WorkKind, relation: Relation) -> WorkItem:
    labels = tuple(
        str(label["name"] if isinstance(label, dict) else label)
        for label in payload.get("labels") or ()
    )
    milestone = payload.get("milestone")
    references = payload.get("references") or {}
    sigil = "!" if kind == "merge_request" else "#"
    updated_at = _dt(payload["updated_at"])
    if updated_at is None:
        raise ValueError("updated_at is empty")
    return WorkItem(
        kind=kind,
        gitlab_id=int(payload["id"]),
        iid=int(payload["iid"]),
        project_id=int(payload["project_id"]) if payload.get("project_id") is not None else None,
        reference=str(references.get("full") or f"{sigil}{payload['iid']}"),
        title=str(payload["title"]),
        state=str(payload["state"]),
        issue_type=payload.get("issue_type") if kind == "issue" else None,
        labels=labels,
        milestone=str(milestone["title"]) if isinstance(milestone, dict) else None,
        priority=priority_from_labels(labels),
        due_date=_date(payload.get("due_date")),
        created_at=_dt(payload.get("created_at")),
        updated_at=updated_at,
        closed_at=_dt(payload.get("merged_at") or payload.get("closed_at")),
        web_url=payload.get("web_url") or None,
        author=(payload.get("author") or {}).get("username"),
        assignees=tuple(str(a["username"]) for a in payload.get("assignees") or ()),
        draft=bool(payload.get("draft") or payload.get("work_in_progress")),
        relation=relation,
    )


TARGET_NAMES = {
    "Issue": ("issue", "#", "/-/issues/"),
    "WorkItem": ("work item", "#", "/-/work_items/"),
    "MergeRequest": ("merge request", "!", "/-/merge_requests/"),
    "Milestone": ("milestone", "%", "/-/milestones/"),
}
NOTEABLE_PATHS = {
    "Issue": "/-/issues/",
    "WorkItem": "/-/work_items/",
    "MergeRequest": "/-/merge_requests/",
}


def categorize_event(
    action: str, target_type: str | None, has_push: bool, has_note: bool
) -> ActivityCategory:
    if has_push or action.startswith("pushed"):
        return "push"
    if has_note or action.startswith("commented"):
        return "comment"
    if target_type == "MergeRequest" or action in {"accepted", "approved"}:
        return "merge_request"
    if target_type in {"Issue", "WorkItem", "Epic"}:
        return "issue"
    return "other"


def _push_summary(push: dict[str, Any]) -> tuple[str, str | None]:
    ref = str(push.get("ref") or "")
    ref_type = str(push.get("ref_type") or "branch")
    action = str(push.get("action") or "pushed")
    count = int(push.get("commit_count") or 0)
    title = push.get("commit_title")
    path = f"/-/commits/{ref}" if ref_type == "branch" else f"/-/tags/{ref}"
    if action == "removed":
        return f"Deleted {ref_type} {ref}", None
    if action == "created":
        text = f"Pushed new {ref_type} {ref}"
    else:
        plural = "commit" if count == 1 else "commits"
        text = f"Pushed {count} {plural} to {ref}"
    if title:
        text = f"{text}: {title}"
    return text, path


def parse_event(payload: dict[str, Any]) -> ActivityEvent:
    action = str(payload.get("action_name") or "acted")
    target_type = payload.get("target_type") or None
    target_title = payload.get("target_title") or None
    target_iid = payload.get("target_iid")
    push = payload.get("push_data") if isinstance(payload.get("push_data"), dict) else None
    note = payload.get("note") if isinstance(payload.get("note"), dict) else None
    created_at = _dt(payload["created_at"])
    if created_at is None:
        raise ValueError("created_at is empty")
    category = categorize_event(action, target_type, push is not None, note is not None)
    url_path: str | None = None
    if push is not None:
        summary, url_path = _push_summary(push)
    elif note is not None:
        noteable_type = str(note.get("noteable_type") or target_type or "")
        noteable_iid = note.get("noteable_iid")
        human, sigil, _ = TARGET_NAMES.get(noteable_type, (noteable_type.lower(), "", ""))
        ref = f" {sigil}{noteable_iid}" if noteable_iid is not None else ""
        summary = f"Commented on {human}{ref}".rstrip()
        if target_title:
            summary = f"{summary}: {target_title}"
        base = NOTEABLE_PATHS.get(noteable_type)
        if base and noteable_iid is not None:
            url_path = f"{base}{noteable_iid}#note_{note.get('id')}"
        elif noteable_type == "Commit" and note.get("commit_id"):
            url_path = f"/-/commit/{note['commit_id']}#note_{note.get('id')}"
    elif target_type in TARGET_NAMES:
        human, sigil, base = TARGET_NAMES[target_type]
        ref = f" {sigil}{target_iid}" if target_iid is not None else ""
        summary = f"{action.capitalize()} {human}{ref}"
        if target_title:
            summary = f"{summary}: {target_title}"
        if target_iid is not None:
            url_path = f"{base}{target_iid}"
    else:
        kind = f" {target_type.lower()}" if target_type else ""
        summary = f"{action.capitalize()}{kind}"
        if target_title:
            summary = f"{summary}: {target_title}"
    return ActivityEvent(
        id=int(payload["id"]),
        project_id=int(payload["project_id"]) if payload.get("project_id") is not None else None,
        action_name=action,
        target_type=target_type,
        target_title=target_title,
        category=category,
        summary=summary,
        created_at=created_at,
        url_path=url_path,
    )


def gid_to_int(gid: Any) -> int | None:
    """``gid://gitlab/Project/12`` -> 12."""
    if gid in (None, ""):
        return None
    return int(str(gid).rsplit("/", 1)[-1])


def parse_timelog(payload: dict[str, Any]) -> Timelog:
    spent_at = _dt(payload["spentAt"])
    if spent_at is None:
        raise ValueError("spentAt is empty")
    issue = payload.get("issue") or None
    merge_request = payload.get("mergeRequest") or None
    project = payload.get("project") or {}
    target: dict[str, Any] | None = issue or merge_request
    kind: WorkKind | None = "issue" if issue else ("merge_request" if merge_request else None)
    iid = target.get("iid") if target else None
    return Timelog(
        id=int(gid_to_int(payload["id"]) or 0),
        spent_at=spent_at,
        seconds=int(payload["timeSpent"]),
        summary=payload.get("summary") or None,
        project_id=gid_to_int(project.get("id")),
        project_path=project.get("fullPath"),
        target_kind=kind,
        target_iid=int(iid) if iid is not None else None,
        target_title=target.get("title") if target else None,
        web_url=(target.get("webUrl") if target else None) or project.get("webUrl"),
    )

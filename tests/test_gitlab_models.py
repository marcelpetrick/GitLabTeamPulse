from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from gitlab_team_pulse.gitlab.errors import GitLabResponseError
from gitlab_team_pulse.gitlab.models import (
    categorize_event,
    classify_account,
    gid_to_int,
    guarded,
    parse_event,
    parse_project,
    parse_timelog,
    parse_user,
    parse_work_item,
    priority_from_labels,
)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"user_type": "human"}, "human"),
        ({"user_type": "project_bot"}, "bot"),
        ({"user_type": "security_policy_bot"}, "bot"),
        ({"user_type": "service_account"}, "service"),
        ({"user_type": "something_new"}, "unknown"),
        ({"bot": True}, "bot"),
        ({"bot": False}, "human"),
        ({}, "unknown"),
        ({"user_type": "", "bot": True}, "bot"),
    ],
)
def test_classify_account(payload: dict[str, Any], expected: str) -> None:
    assert classify_account(payload) == expected


def test_parse_user_full_and_minimal() -> None:
    user = parse_user(
        {
            "id": 5,
            "username": "alex",
            "name": "Alex A",
            "state": "Blocked",
            "avatar_url": "https://g/a.png",
            "web_url": "https://g/alex",
            "created_at": "2024-01-02T03:04:05.000Z",
            "bot": False,
            "is_admin": True,
        }
    )
    assert user.state == "blocked"
    assert user.account_type == "human"
    assert user.created_at == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert user.is_admin is True
    minimal = parse_user({"id": "6", "username": "svc"})
    assert minimal.name == "svc"
    assert minimal.state == "unknown"
    assert minimal.created_at is None
    assert minimal.is_admin is None


def test_parse_project() -> None:
    project = parse_project({"id": 3, "path_with_namespace": "grp/app", "web_url": "u"})
    assert project.name == "grp/app"
    assert project.web_url == "u"


def test_priority_from_labels() -> None:
    assert priority_from_labels(("bug", "Prio::1")) == "1"
    assert priority_from_labels(("priority::high",)) == "high"
    assert priority_from_labels(("priority::", "team::a", "plain")) is None


ISSUE = {
    "id": 101,
    "iid": 7,
    "project_id": 3,
    "title": "Fix login",
    "state": "closed",
    "labels": ["bug", {"name": "priority::high"}],
    "milestone": {"title": "M1"},
    "due_date": "2026-10-01",
    "created_at": "2026-09-01T10:00:00Z",
    "updated_at": "2026-09-20T10:00:00Z",
    "closed_at": "2026-09-20T10:00:00Z",
    "web_url": "https://g/grp/app/-/issues/7",
    "author": {"username": "bob"},
    "assignees": [{"username": "alex"}],
    "issue_type": "task",
    "references": {"full": "grp/app#7"},
}


def test_parse_issue_keeps_closed_state_and_metadata() -> None:
    item = parse_work_item(ISSUE, "issue", "assignee")
    assert item.state == "closed"
    assert item.labels == ("bug", "priority::high")
    assert item.priority == "high"
    assert item.milestone == "M1"
    assert item.due_date == date(2026, 10, 1)
    assert item.issue_type == "task"
    assert item.reference == "grp/app#7"
    assert item.author == "bob"
    assert item.assignees == ("alex",)


def test_parse_merge_request_minimal() -> None:
    item = parse_work_item(
        {
            "id": 9,
            "iid": 2,
            "title": "Draft: feature",
            "state": "merged",
            "updated_at": "2026-09-20T10:00:00Z",
            "merged_at": "2026-09-20T11:00:00Z",
            "draft": True,
            "issue_type": "ignored",
        },
        "merge_request",
        "reviewer",
    )
    assert item.reference == "!2"
    assert item.project_id is None
    assert item.issue_type is None
    assert item.draft is True
    assert item.closed_at == datetime(2026, 9, 20, 11, tzinfo=UTC)
    assert item.relation == "reviewer"


def test_parse_work_item_requires_updated_at() -> None:
    with pytest.raises(GitLabResponseError):
        guarded(
            "issue", lambda p: parse_work_item(p, "issue", "assignee"), {**ISSUE, "updated_at": ""}
        )


def test_guarded_rejects_non_objects_and_bad_payloads() -> None:
    with pytest.raises(GitLabResponseError, match="expected an object"):
        guarded("user", parse_user, [1])
    with pytest.raises(GitLabResponseError, match="KeyError"):
        guarded("user", parse_user, {"id": 1})


def test_guarded_passes_through_response_errors() -> None:
    def boom(_: dict[str, Any]) -> None:
        raise GitLabResponseError("inner")

    with pytest.raises(GitLabResponseError, match="inner"):
        guarded("x", boom, {})


@pytest.mark.parametrize(
    ("action", "target", "push", "note", "expected"),
    [
        ("pushed to", None, True, False, "push"),
        ("pushed new", None, False, False, "push"),
        ("commented on", "Note", False, True, "comment"),
        ("accepted", "MergeRequest", False, False, "merge_request"),
        ("approved", None, False, False, "merge_request"),
        ("opened", "Issue", False, False, "issue"),
        ("closed", "WorkItem", False, False, "issue"),
        ("joined", None, False, False, "other"),
    ],
)
def test_categorize_event(
    action: str, target: str | None, push: bool, note: bool, expected: str
) -> None:
    assert categorize_event(action, target, push, note) == expected


def event(**extra: Any) -> dict[str, Any]:
    return {"id": 1, "project_id": 3, "created_at": "2026-09-20T10:00:00Z", **extra}


def test_push_events() -> None:
    pushed = parse_event(
        event(
            action_name="pushed to",
            push_data={
                "commit_count": 2,
                "ref": "main",
                "ref_type": "branch",
                "commit_title": "Fix",
            },
        )
    )
    assert pushed.summary == "Pushed 2 commits to main: Fix"
    assert pushed.url_path == "/-/commits/main"
    one = parse_event(event(action_name="pushed to", push_data={"commit_count": 1, "ref": "x"}))
    assert one.summary == "Pushed 1 commit to x"
    new = parse_event(
        event(
            action_name="pushed new",
            push_data={"action": "created", "ref": "v1", "ref_type": "tag"},
        )
    )
    assert new.summary == "Pushed new tag v1"
    assert new.url_path == "/-/tags/v1"
    deleted = parse_event(
        event(action_name="deleted", push_data={"action": "removed", "ref": "old"})
    )
    assert deleted.summary == "Deleted branch old"
    assert deleted.url_path is None
    assert deleted.category == "push"


def test_comment_events() -> None:
    issue_note = parse_event(
        event(
            action_name="commented on",
            target_type="Note",
            target_title="Fix login",
            note={"id": 55, "noteable_type": "Issue", "noteable_iid": 7},
        )
    )
    assert issue_note.summary == "Commented on issue #7: Fix login"
    assert issue_note.url_path == "/-/issues/7#note_55"
    commit_note = parse_event(
        event(
            action_name="commented on",
            note={"id": 56, "noteable_type": "Commit", "commit_id": "abc"},
        )
    )
    assert commit_note.summary == "Commented on commit"
    assert commit_note.url_path == "/-/commit/abc#note_56"
    odd = parse_event(
        event(action_name="commented on", note={"id": 57, "noteable_type": "Snippet"})
    )
    assert odd.url_path is None


def test_target_and_other_events() -> None:
    opened = parse_event(
        event(action_name="opened", target_type="MergeRequest", target_iid=4, target_title="Add X")
    )
    assert opened.summary == "Opened merge request !4: Add X"
    assert opened.url_path == "/-/merge_requests/4"
    no_iid = parse_event(event(action_name="closed", target_type="Milestone"))
    assert no_iid.summary == "Closed milestone"
    assert no_iid.url_path is None
    joined = parse_event({"id": 2, "created_at": "2026-09-20T10:00:00Z", "action_name": "joined"})
    assert joined.summary == "Joined"
    assert joined.project_id is None
    design = parse_event(
        event(action_name="updated", target_type="DesignManagement::Design", target_title="Logo")
    )
    assert design.summary == "Updated designmanagement::design: Logo"


def test_event_requires_created_at() -> None:
    with pytest.raises(GitLabResponseError):
        guarded("event", parse_event, {"id": 1, "created_at": None})


def test_gid_to_int() -> None:
    assert gid_to_int("gid://gitlab/Project/12") == 12
    assert gid_to_int(None) is None
    assert gid_to_int("") is None


def test_parse_timelog_variants() -> None:
    issue_log = parse_timelog(
        {
            "id": "gid://gitlab/Timelog/9",
            "spentAt": "2026-09-20T08:00:00Z",
            "timeSpent": 3600,
            "summary": "Pairing",
            "project": {
                "id": "gid://gitlab/Project/3",
                "fullPath": "grp/app",
                "webUrl": "https://g/grp/app",
            },
            "issue": {"iid": "7", "title": "Fix login", "webUrl": "https://g/grp/app/-/issues/7"},
        }
    )
    assert issue_log.id == 9
    assert issue_log.target_kind == "issue"
    assert issue_log.target_iid == 7
    assert issue_log.project_id == 3
    assert issue_log.web_url == "https://g/grp/app/-/issues/7"
    mr_log = parse_timelog(
        {
            "id": "gid://gitlab/Timelog/10",
            "spentAt": "2026-09-20T08:00:00Z",
            "timeSpent": 60,
            "mergeRequest": {"iid": 2, "title": "MR"},
            "project": {"webUrl": "https://g/p"},
        }
    )
    assert mr_log.target_kind == "merge_request"
    assert mr_log.web_url == "https://g/p"
    bare = parse_timelog(
        {"id": "gid://gitlab/Timelog/11", "spentAt": "2026-09-20T08:00:00Z", "timeSpent": 1}
    )
    assert bare.target_kind is None
    assert bare.target_iid is None
    assert bare.summary is None
    with pytest.raises(GitLabResponseError):
        guarded("timelog", parse_timelog, {"id": "x", "spentAt": "", "timeSpent": 1})


def test_parse_epic() -> None:
    from gitlab_team_pulse.gitlab.models import parse_epic

    epic = parse_epic(
        {
            "id": "gid://gitlab/WorkItem/77",
            "iid": "3",
            "title": "Roadmap",
            "state": "OPEN",
            "updatedAt": "2026-09-20T10:00:00Z",
            "reference": "grp&3",
            "widgets": [
                {"type": "ASSIGNEES", "assignees": {"nodes": [{"username": "alex"}]}},
                {"type": "LABELS", "labels": {"nodes": [{"title": "prio::2"}]}},
                {"type": "MILESTONE", "milestone": {"title": "M9"}},
                {"type": "START_AND_DUE_DATE", "dueDate": "2026-12-01"},
                "junk",
            ],
        }
    )
    assert (epic.kind, epic.gitlab_id, epic.iid, epic.state) == ("epic", 77, 3, "opened")
    assert epic.assignees == ("alex",)
    assert epic.priority == "2"
    assert epic.milestone == "M9"
    assert epic.due_date == date(2026, 12, 1)
    closed = parse_epic(
        {
            "id": "gid://gitlab/WorkItem/1",
            "iid": 1,
            "title": "x",
            "state": "CLOSED",
            "updatedAt": "2026-09-20T10:00:00Z",
        }
    )
    assert closed.state == "closed"
    assert closed.reference == "&1"
    assert closed.labels == ()
    with pytest.raises(GitLabResponseError):
        guarded("epic", parse_epic, {"id": "x", "iid": 1, "title": "t", "updatedAt": ""})

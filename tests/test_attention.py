"""The "needs attention" list, milestone progress and the new work item fields in the read model."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import store
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.dashboard import DashboardReader, merge_problems
from gitlab_team_pulse.gitlab.models import GitLabProject, GitLabUser, MilestoneRef, WorkItem
from gitlab_team_pulse.models import User, WorkItemRecord
from gitlab_team_pulse.sync import RuntimeState

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 15)
ALEX, SAM = 2, 3
SPRINT = MilestoneRef(
    id=71,
    iid=1,
    project_id=1,
    title="2026.10",
    start_date=date(2026, 10, 1),
    due_date=date(2026, 10, 21),
    web_url="http://fake.gitlab/platform/api/-/milestones/1",
)


def item(gitlab_id: int, **fields: Any) -> WorkItem:
    kind = fields.pop("kind", "issue")
    defaults: dict[str, Any] = {
        "kind": kind,
        "gitlab_id": gitlab_id,
        "iid": gitlab_id,
        "project_id": 1,
        "reference": f"platform/api{'!' if kind == 'merge_request' else '#'}{gitlab_id}",
        "title": f"Item {gitlab_id}",
        "state": "opened",
        "updated_at": NOW - timedelta(hours=2),
        "created_at": NOW - timedelta(days=3),
    }
    return WorkItem(**{**defaults, **fields})


def build(session_factory: sessionmaker[Session], work: dict[int, list[WorkItem]]) -> None:
    with session_factory() as session:
        store.upsert_users(
            session,
            [
                GitLabUser(id=ALEX, username="alex", name="Alex Berger"),
                GitLabUser(id=SAM, username="sam", name="Sam Novak"),
                GitLabUser(id=9, username="outsider", name="Out Sider"),
            ],
            NOW,
        )
        for uid in (ALEX, SAM):
            store.set_selected(session, uid, True, NOW)
        store.upsert_project(
            session, GitLabProject(id=1, name="api", path_with_namespace="platform/api"), NOW
        )
        for uid, items in work.items():
            store.replace_user_work(session, uid, items, NOW)
        session.commit()


def overview(session_factory: sessionmaker[Session], settings: Settings) -> dict[str, Any]:
    with session_factory() as session:
        users = store.selected_users(session)
        return DashboardReader(session, settings, RuntimeState(), NOW).overview(users)


def reasons(payload: dict[str, Any], reference: str) -> dict[str, dict[str, Any]]:
    row = next(i for i in payload["attention"]["items"] if i["reference"] == reference)
    return {r["code"]: r for r in row["reasons"]}


def test_attention_reasons(session_factory: sessionmaker[Session], settings: Settings) -> None:
    build(
        session_factory,
        {
            ALEX: [
                item(1, updated_at=NOW - timedelta(days=9), due_date=TODAY - timedelta(days=2)),
                item(2, due_date=TODAY + timedelta(days=3), milestone_ref=SPRINT),
                item(3, due_date=TODAY + timedelta(days=30)),
                item(4, state="closed", updated_at=NOW - timedelta(days=60)),
                item(5, kind="merge_request", reviewers=()),
                item(6, kind="merge_request", reviewers=("outsider",), has_conflicts=True),
                item(7, kind="merge_request", reviewers=("sam",), merge_status="ci_must_pass"),
                item(8, kind="merge_request", draft=True, reviewers=()),
            ],
            SAM: [
                item(
                    7,
                    kind="merge_request",
                    relation="reviewer",
                    reviewers=("sam",),
                    merge_status="ci_must_pass",
                )
            ],
        },
    )
    payload = overview(session_factory, settings)
    attention = payload["attention"]
    refs = [i["reference"] for i in attention["items"]]
    assert refs[0] == "platform/api#1"  # errors first
    assert "platform/api#3" not in refs
    assert "platform/api#4" not in refs
    assert "platform/api!8" not in refs  # drafts are not chased
    assert reasons(payload, "platform/api#1") == {
        "overdue": {"code": "overdue", "severity": "error", "days": 2},
        "stalled": {"code": "stalled", "severity": "error", "days": 9},
    }
    assert reasons(payload, "platform/api#2")["due_soon"]["days"] == 3
    assert reasons(payload, "platform/api!5") == {
        "no_reviewer": {"code": "no_reviewer", "severity": "warning"}
    }
    six = reasons(payload, "platform/api!6")
    assert six["outside_reviewer"]["reviewers"] == ["outsider"]
    assert six["cannot_merge"]["problems"] == ["conflict"]
    seven = next(i for i in attention["items"] if i["reference"] == "platform/api!7")
    assert {p["username"] for p in seven["people"]} == {"alex", "sam"}  # one row per item
    assert set(reasons(payload, "platform/api!7")) == {"review", "cannot_merge"}
    assert reasons(payload, "platform/api!7")["review"]["severity"] == "warning"
    assert attention["counts"] == {
        "stalled": 1,
        "overdue": 1,
        "due_soon": 1,
        "review": 1,
        "cannot_merge": 2,
        "no_reviewer": 1,
        "outside_reviewer": 1,
    }
    card = next(c for c in payload["cards"] if c["user"]["id"] == ALEX)
    assert (card["counts"]["stalled"], card["counts"]["overdue"]) == (1, 1)
    first = next(w for w in card["work"] if w["reference"] == "platform/api#1")
    assert first["stalled_days"] == 9


def test_nothing_needs_attention(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    build(session_factory, {ALEX: [item(1)], SAM: []})
    payload = overview(session_factory, settings)
    assert payload["attention"]["items"] == []
    assert payload["milestones"] == []
    with session_factory() as session:
        empty = DashboardReader(session, settings, RuntimeState(), NOW).overview([])
    assert empty["cards"] == []
    assert empty["attention"]["items"] == []


def test_milestones_of_open_work_with_progress(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    later = MilestoneRef(id=72, iid=2, project_id=1, title="2026.11", due_date=date(2026, 11, 20))
    undated = MilestoneRef(id=73, iid=3, project_id=1, title="Backlog")
    closed = MilestoneRef(id=74, iid=4, project_id=1, title="Old", state="closed")
    build(
        session_factory,
        {
            ALEX: [
                item(1, milestone_ref=later),
                item(2, milestone_ref=SPRINT),
                item(3, milestone_ref=undated),
                item(4, milestone_ref=closed),
                item(5, milestone_ref=SPRINT, state="closed"),
            ],
            SAM: [item(6, milestone_ref=SPRINT)],
        },
    )
    issues = [
        item(1, time_estimate=7200, time_spent=3600, assignees=("alex",)),
        item(2, time_estimate=0, due_date=TODAY - timedelta(days=1)),
        item(3, state="closed", closed_at=NOW - timedelta(days=2), time_estimate=3600),
    ]
    with session_factory() as session:
        store.set_milestone_stats(session, SPRINT.id, store.milestone_stats(issues, TODAY), NOW)
        store.set_milestone_stats(session, 999, {}, NOW)  # unknown milestones are ignored
        session.commit()
    milestones = overview(session_factory, settings)["milestones"]
    assert [m["title"] for m in milestones] == ["2026.10", "2026.11", "Backlog"]
    sprint = milestones[0]
    assert sprint["days_left"] == 6
    assert sprint["monitored_open"] == 2
    assert sprint["monitored_people"] == ["Alex Berger", "Sam Novak"]
    assert sprint["project"]["path"] == "platform/api"
    assert sprint["web_url"] == SPRINT.web_url
    assert sprint["stats"] == {
        "total": 3,
        "closed": 1,
        "estimate": 10800,
        "spent": 3600,
        "remaining": 3600,
        "open_unestimated": 1,
        "open_overdue": 1,
        "open_by_assignee": {"alex": 1, "": 1},
        "closed_dates": [(NOW - timedelta(days=2)).date().isoformat()],
    }
    assert milestones[2]["days_left"] is None
    assert milestones[2]["stats"] is None
    capped = overview(session_factory, settings.model_copy(update={"milestones_max": 1}))
    assert [m["title"] for m in capped["milestones"]] == ["2026.10"]


def test_merge_problems() -> None:
    def record(**fields: Any) -> WorkItemRecord:
        defaults: dict[str, Any] = {
            "kind": "merge_request",
            "state": "opened",
            "has_conflicts": False,
            "merge_status": None,
            "discussions_resolved": None,
        }
        return WorkItemRecord(**{**defaults, **fields})

    assert merge_problems(record(kind="issue")) == []
    assert merge_problems(record(state="merged", has_conflicts=True)) == []
    assert merge_problems(record(merge_status="mergeable")) == []
    assert merge_problems(record(merge_status="conflict", has_conflicts=True)) == ["conflict"]
    assert merge_problems(record(merge_status="need_rebase")) == ["need_rebase"]
    assert merge_problems(record(discussions_resolved=False)) == ["threads"]
    assert merge_problems(
        record(merge_status="discussions_not_resolved", discussions_resolved=False)
    ) == ["threads"]


def test_new_work_fields_are_stored(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    build(
        session_factory,
        {
            ALEX: [
                item(
                    1,
                    kind="merge_request",
                    merged_at=NOW,
                    state="merged",
                    time_estimate=60,
                    time_spent=120,
                    reviewers=("sam",),
                    merge_status="not_open",
                    milestone_ref=SPRINT,
                )
            ]
        },
    )
    payload = overview(session_factory, settings)
    work = payload["cards"][0]["work"][0]
    assert (work["merged_at"], work["time_estimate"], work["time_spent"]) == (NOW, 60, 120)
    assert work["reviewers"] == ["sam"]
    assert work["milestone_id"] == SPRINT.id
    assert work["stalled_days"] is None
    with session_factory() as session:
        assert session.get(User, ALEX) is not None

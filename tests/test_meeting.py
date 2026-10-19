"""Meeting summary: levels and fallbacks, item order and reasons, risks, Markdown and the API."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import meeting
from gitlab_team_pulse.app import AppContext, create_app
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.models import (
    ActivityDaily,
    Milestone,
    Outcome,
    Project,
    TimelogDaily,
    TimelogRecord,
    User,
    WorkItemAssignee,
    WorkItemRecord,
)
from gitlab_team_pulse.sync import RuntimeState

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
_ids = count(1)


def user(session: Session, uid: int, username: str, name: str, *, selected: bool = True) -> User:
    row = User(
        id=uid,
        username=username,
        name=name,
        selected=selected,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def work(
    session: Session,
    owner: User,
    *,
    relation: str = "assignee",
    kind: str = "issue",
    state: str = "closed",
    title: str = "Item",
    iid: int | None = None,
    project_id: int = 10,
    finished: datetime | None = NOW - timedelta(days=1),
    **fields: Any,
) -> WorkItemRecord:
    number = next(_ids)
    iid = iid if iid is not None else number
    sign = "!" if kind == "merge_request" else "#"
    values: dict[str, Any] = {
        "kind": kind,
        "gitlab_id": 1000 + number,
        "iid": iid,
        "project_id": project_id,
        "reference": f"group/app{sign}{iid}",
        "title": title,
        "state": state,
        "labels": [],
        "assignees": [owner.username],
        "updated_at": finished or NOW - timedelta(days=1),
        "created_at": (finished or NOW) - timedelta(days=3),
        "refreshed_at": NOW,
        "web_url": f"https://gitlab.example.com/group/app/-/issues/{iid}",
        "reviewers": [],
    }
    if state == "merged":
        values["merged_at"] = finished
    if state == "closed":
        values["closed_at"] = finished
    values.update(fields)
    record = WorkItemRecord(**values)
    session.add(record)
    session.flush()
    session.add(
        WorkItemAssignee(
            work_item_id=record.id, user_id=owner.id, relation=relation, observed_at=NOW
        )
    )
    return record


def build(session: Session, settings: Settings, users: list[User], days: int = 7) -> Any:
    session.flush()
    return meeting.MeetingSummary(session, settings, RuntimeState(), NOW).build(users, days)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as found:
        found.add(
            Project(
                id=10,
                name="app",
                path_with_namespace="group/app",
                web_url="https://gitlab.example.com/group/app",
                refreshed_at=NOW,
            )
        )
        yield found


@pytest.fixture
def cfg(settings: Settings) -> Settings:
    return settings.model_copy(update={"gitlab_url": "https://gitlab.example.com"})


# ---------------------------------------------------------------------- delivered


def test_delivered_items_are_ordered_by_facts_and_explain_why(
    session: Session, cfg: Settings
) -> None:
    alex = user(session, 2, "alex", "Alex")
    session.add(
        Milestone(
            id=7,
            iid=1,
            project_id=10,
            title="2026.10",
            state="active",
            due_date=TODAY + timedelta(days=5),
            seen_at=NOW,
        )
    )
    work(session, alex, title="Plain chore")
    work(
        session,
        alex,
        title="Outage fix",
        issue_type="incident",
        priority="1",
        labels=["Prio::1", "customer"],
        milestone="2026.10",
        milestone_id=7,
        due_date=TODAY,
        time_spent=20 * 3600,
        created_at=NOW - timedelta(days=120),
    )
    work(
        session,
        alex,
        title="Login bug",
        labels=["type::bug", "security"],
        priority="medium",
        milestone="Backlog",
        due_date=TODAY - timedelta(days=4),
        time_estimate=5 * 3600,
        created_at=NOW - timedelta(days=40),
    )
    work(session, alex, title="Shiny thing", labels=["feature"])
    work(session, alex, title="Too old", finished=NOW - timedelta(days=9))
    work(session, alex, title="Still open", state="opened", finished=None)
    result = build(session, cfg, [alex])
    person = result["people"][0]
    assert person["level"] == "delivered"
    titles = [i["title"] for i in person["delivered"]]
    assert titles == ["Outage fix", "Login bug", "Shiny thing", "Plain chore"]
    first, second = person["delivered"][0], person["delivered"][1]
    assert first["reasons"] == [
        "Prio 1",
        "incident",
        "customer",
        "milestone 2026.10",
        "on time",
        "large: 20h 00m",
        "open 119 d",
    ]
    assert first["project"] == "group/app"
    assert first["web_url"].startswith("https://gitlab.example.com/")
    # A backlog milestone is no plan; done late still counts as done.
    assert second["reasons"] == [
        "Prio medium",
        "security",
        "bug fix",
        "done 3 d after due date",
        "5h 00m",
        "open 39 d",
    ]
    assert person["headline"].startswith("Delivered 4: #")
    assert "Outage fix (Prio 1)" in person["headline"]
    assert "; and 1 more." in person["headline"]


def test_a_merge_request_that_delivers_an_issue_is_folded_into_it(
    session: Session, cfg: Settings
) -> None:
    alex = user(session, 2, "alex", "Alex")
    work(session, alex, title="Export CSV", iid=12, time_estimate=3600)
    work(
        session,
        alex,
        kind="merge_request",
        state="merged",
        title='Draft: Resolve "Export CSV"',
        time_spent=7200,
    )
    work(session, alex, title="Fix import", iid=13)
    work(session, alex, kind="merge_request", state="merged", title="Faster import (#13)")
    work(session, alex, kind="merge_request", state="merged", title="Other repo #13", project_id=11)
    work(session, alex, kind="merge_request", state="merged", title="Mentions #99 only")
    person = build(session, cfg, [alex])["people"][0]
    by_title = {i["title"]: i for i in person["delivered"]}
    assert set(by_title) == {"Export CSV", "Fix import", "Other repo #13", "Mentions #99 only"}
    assert len(by_title["Export CSV"]["via"]) == 1
    assert by_title["Export CSV"]["via"][0].startswith("group/app!")
    assert len(by_title["Fix import"]["via"]) == 1


def test_outcomes_add_authored_merges_and_fill_missing_records(
    session: Session, cfg: Settings
) -> None:
    alex = user(session, 2, "alex", "Alex")
    record = work(session, alex, kind="merge_request", state="merged", title="Known MR")
    session.add_all(
        [
            Outcome(
                user_id=2,
                kind="merge_request",
                gitlab_id=record.gitlab_id,
                project_id=10,
                iid=record.iid,
                reference=record.reference,
                title="Known MR",
                finished_at=NOW - timedelta(days=1),
            ),
            Outcome(
                user_id=2,
                kind="merge_request",
                gitlab_id=5,
                project_id=None,
                iid=5,
                reference="other/repo!5",
                title="Authored elsewhere",
                web_url="https://elsewhere.example.com/x",
                created_at=None,
                finished_at=NOW - timedelta(days=2),
                time_estimate=0,
                time_spent=0,
            ),
            Outcome(
                user_id=2,
                kind="issue",
                gitlab_id=6,
                project_id=10,
                iid=6,
                reference="group/app#6",
                title="Too old for the period",
                finished_at=NOW - timedelta(days=30),
                time_estimate=0,
                time_spent=0,
            ),
        ]
    )
    person = build(session, cfg, [alex])["people"][0]
    assert [i["title"] for i in person["delivered"]] == ["Known MR", "Authored elsewhere"]
    elsewhere = person["delivered"][1]
    assert elsewhere["web_url"] is None  # not the configured GitLab
    assert elsewhere["project"] is None


def test_only_five_items_are_listed(session: Session, cfg: Settings) -> None:
    alex = user(session, 2, "alex", "Alex")
    for n in range(7):
        work(session, alex, title=f"Done {n}")
    person = build(session, cfg, [alex])["people"][0]
    assert person["delivered_total"] == 7
    assert len(person["delivered"]) == meeting.DELIVERED_SHOWN
    assert "- … and 2 more delivered" in meeting.markdown(
        {"period": {"label": "x"}, "people": [person]}
    )


# ---------------------------------------------------------------------- fallbacks


def test_unblocked_others_when_nothing_own_was_delivered(session: Session, cfg: Settings) -> None:
    kim = user(session, 3, "kim", "Kim")
    user(session, 4, "mika", "Mika Moreau", selected=False)
    work(
        session,
        kim,
        relation="reviewer",
        kind="merge_request",
        state="merged",
        title="Bump deps",
        author="mika",
    )
    work(
        session,
        kim,
        relation="reviewer",
        kind="merge_request",
        state="merged",
        title="Unknown author",
        author="ghost",
    )
    work(
        session,
        kim,
        relation="reviewer",
        kind="merge_request",
        state="merged",
        title="Own MR",
        author="kim",
    )
    session.add(ActivityDaily(user_id=3, day=TODAY, project_id=10, metric="mr_approved", value=3))
    person = build(session, cfg, [kim])["people"][0]
    assert person["level"] == "unblocked"
    assert {r["author"] for r in person["unblocked"]["merged"]} == {"Mika Moreau", "ghost"}
    assert person["headline"] == (
        "No own delivery in GitLab; unblocked others: reviewed 2 MRs that got merged, 3 approvals."
    )
    approvals_only = user(session, 5, "lee", "Lee")
    session.add(ActivityDaily(user_id=5, day=TODAY, project_id=10, metric="mr_approved", value=1))
    lee = build(session, cfg, [approvals_only])["people"][0]
    assert lee["headline"] == "No own delivery in GitLab; unblocked others: 1 approval."


def test_delivered_headline_mentions_unblocked_others(session: Session, cfg: Settings) -> None:
    kim = user(session, 3, "kim", "Kim")
    work(session, kim, title="Shipped")
    work(
        session,
        kim,
        relation="reviewer",
        kind="merge_request",
        state="merged",
        title="Their MR",
        author="mika",
    )
    person = build(session, cfg, [kim])["people"][0]
    assert person["headline"].endswith("Shipped. Also unblocked 1 MR of others.")
    text = meeting.markdown({"period": {"label": "x"}, "people": [person]})
    assert "🤝 Reviewed" in text
    assert "by mika, merged" in text


def test_in_progress_lists_open_work_by_weight(session: Session, cfg: Settings) -> None:
    sam = user(session, 4, "sam", "Sam")
    opened = NOW - timedelta(days=2)
    work(
        session,
        sam,
        kind="merge_request",
        state="opened",
        finished=None,
        title="New API",
        created_at=opened,
        updated_at=opened,
        reviewers=["kim"],
    )
    work(
        session,
        sam,
        kind="merge_request",
        state="opened",
        finished=None,
        title="Spike",
        draft=True,
        created_at=NOW - timedelta(days=20),
        updated_at=opened,
    )
    work(
        session,
        sam,
        kind="merge_request",
        state="opened",
        finished=None,
        title="Lonely MR",
        created_at=NOW - timedelta(days=20),
        updated_at=opened,
    )
    logged = work(
        session,
        sam,
        state="opened",
        finished=None,
        title="Hard bug",
        labels=["bug"],
        updated_at=NOW - timedelta(days=20),
        created_at=NOW - timedelta(days=60),
    )
    work(
        session,
        sam,
        state="opened",
        finished=None,
        title="Fresh issue",
        created_at=opened,
        updated_at=opened,
    )
    work(
        session,
        sam,
        state="opened",
        finished=None,
        title="Untouched",
        updated_at=NOW - timedelta(days=40),
    )
    session.add(
        TimelogRecord(
            id=1,
            user_id=4,
            project_id=10,
            project_path="group/app",
            spent_at=NOW - timedelta(days=1),
            seconds=5400,
            target_kind="issue",
            target_iid=logged.iid,
            fetched_at=NOW,
        )
    )
    person = build(session, cfg, [sam])["people"][0]
    assert person["level"] == "in_progress"
    statuses = {r["title"]: r["status"] for r in meeting_rows(person)}
    assert statuses == {"Hard bug": "worked on", "New API": "opened, in review", "Spike": "draft"}
    assert person["progress_total"] == 5
    assert person["progress"][0]["logged_seconds"] == 5400
    assert person["headline"].startswith("In progress: #")
    assert person["logged_seconds"] == 5400


def meeting_rows(person: dict[str, Any]) -> list[dict[str, Any]]:
    return list(person["progress"])


def test_discussion_last_delivery_and_nothing(session: Session, cfg: Settings) -> None:
    ana = user(session, 5, "ana", "Ana")
    session.add_all(
        [
            ActivityDaily(user_id=5, day=TODAY, project_id=10, metric="issue_opened", value=2),
            ActivityDaily(user_id=5, day=TODAY, project_id=10, metric="issue_commented", value=1),
            ActivityDaily(
                user_id=5,
                day=TODAY - timedelta(days=30),
                project_id=10,
                metric="mr_commented",
                value=9,
            ),
        ]
    )
    bo = user(session, 6, "bo", "Bo")
    work(session, bo, title="Earlier win", finished=NOW - timedelta(days=12))
    session.add(
        Outcome(
            user_id=6,
            kind="issue",
            gitlab_id=77,
            project_id=10,
            iid=77,
            reference="group/app#77",
            title="Even earlier",
            finished_at=NOW - timedelta(days=20),
            time_estimate=0,
            time_spent=0,
        )
    )
    cy = user(session, 7, "cy", "Cy")
    work(session, cy, title="Ancient", finished=NOW - timedelta(days=200))
    people = build(session, cfg, [ana, bo, cy])["people"]
    assert [p["level"] for p in people] == ["discussion", "last_delivery", "none"]
    assert people[0]["headline"] == "Discussion and triage: 2 issues opened, 1 comment."
    assert people[1]["last_delivery"]["days_ago"] == 12
    assert people[1]["headline"].startswith(
        "Nothing delivered in GitLab in this period; last delivery: #"
    )
    assert people[1]["headline"].endswith("Earlier win, 12 days ago.")
    assert "absence or work outside GitLab" in people[2]["headline"]
    only_comments = user(session, 8, "dee", "Dee")
    session.add(ActivityDaily(user_id=8, day=TODAY, project_id=10, metric="mr_commented", value=1))
    dee = build(session, cfg, [only_comments])["people"][0]
    assert dee["headline"] == "Discussion and triage: 1 comment."


# ---------------------------------------------------------------------- risks, hours, markdown


def test_risks_come_from_the_attention_rules(session: Session, cfg: Settings) -> None:
    alex = user(session, 2, "alex", "Alex")
    work(
        session,
        alex,
        state="opened",
        finished=None,
        title="Late",
        due_date=TODAY - timedelta(days=2),
        updated_at=NOW - timedelta(days=10),
    )
    work(
        session,
        alex,
        state="opened",
        finished=None,
        title="Soon",
        due_date=TODAY,
        updated_at=NOW,
    )
    work(
        session,
        alex,
        state="opened",
        finished=None,
        title="Next week",
        due_date=TODAY + timedelta(days=3),
        updated_at=NOW,
    )
    work(
        session,
        alex,
        kind="merge_request",
        state="opened",
        finished=None,
        title="Conflicted",
        has_conflicts=True,
        reviewers=["kim"],
        updated_at=NOW,
    )
    work(
        session,
        alex,
        kind="merge_request",
        state="opened",
        finished=None,
        title="Alone",
        updated_at=NOW,
    )
    work(
        session,
        alex,
        relation="reviewer",
        kind="merge_request",
        state="opened",
        finished=None,
        title="Their MR",
        created_at=NOW - timedelta(days=4),
        updated_at=NOW,
        reviewers=["alex"],
    )
    person = build(session, cfg, [alex])["people"][0]
    assert person["risks_total"] == 6
    assert len(person["risks"]) == meeting.LINES_SHOWN
    assert person["risks"][0]["title"] == "Late"
    assert person["risks"][0]["reasons"] == ["overdue by 2 d", "no update for 10 d"]
    assert person["risks"][0]["severity"] == "error"
    reasons = [
        meeting._reason_text(r)
        for r in [
            {"code": "due_soon", "days": 0},
            {"code": "due_soon", "days": 3},
            {"code": "review", "days": 4},
            {"code": "cannot_merge", "problems": ["conflict", "need_rebase"]},
            {"code": "no_reviewer"},
            {"code": "something_else"},
        ]
    ]
    assert reasons == [
        "due today",
        "due in 3 d",
        "waits for this review for 4 d",
        "cannot merge: conflict, need rebase",
        "no reviewer",
        "something else",
    ]


def test_hours_use_the_larger_of_history_and_raw_entries(session: Session, cfg: Settings) -> None:
    alex = user(session, 2, "alex", "Alex")
    kim = user(session, 3, "kim", "Kim")
    session.add_all(
        [
            TimelogDaily(user_id=2, day=TODAY, project_id=10, seconds=7200),
            TimelogDaily(user_id=2, day=TODAY - timedelta(days=10), project_id=10, seconds=999),
            TimelogRecord(
                id=9,
                user_id=3,
                project_id=None,
                spent_at=NOW - timedelta(hours=2),
                seconds=1800,
                fetched_at=NOW,
            ),
        ]
    )
    people = build(session, cfg, [alex, kim])["people"]
    assert [p["logged_seconds"] for p in people] == [7200, 1800]


def test_markdown_lists_every_section(session: Session, cfg: Settings) -> None:
    alex = user(session, 2, "alex", "Alex")
    work(session, alex, title="Shipped", labels=["feature"])
    work(session, alex, kind="merge_request", state="merged", title='Resolve "Shipped"')
    work(
        session,
        alex,
        state="opened",
        finished=None,
        title="Next up",
        updated_at=NOW,
        due_date=TODAY - timedelta(days=1),
    )
    session.add_all(
        [
            ActivityDaily(user_id=2, day=TODAY, project_id=10, metric="mr_approved", value=1),
            ActivityDaily(user_id=2, day=TODAY, project_id=10, metric="mr_commented", value=2),
            TimelogDaily(user_id=2, day=TODAY, project_id=10, seconds=3600),
        ]
    )
    result = build(session, cfg, [alex], days=2)
    assert result["period"] == {
        "days": 2,
        "start": TODAY - timedelta(days=1),
        "end": TODAY,
        "label": "14 Oct to 15 Oct 2026",
    }
    text = result["markdown"]
    assert text.startswith("## Meeting summary · 14 Oct to 15 Oct 2026\n\n### Alex\n")
    assert "- ✅ [group/app#" in text
    assert "(with group/app!" in text
    assert "· feature" in text
    assert "- ⏳ [group/app#" in text
    assert "- ⚠️ [group/app#" in text
    assert "overdue by 1 d" in text
    assert "- Context: 1 approval; 2 review comments; 1h 00m logged" in text
    assert result["rules"][0]["name"] == "Delivered"


def test_empty_selection_and_helpers(session: Session, cfg: Settings) -> None:
    result = build(session, cfg, [])
    assert result["people"] == []
    assert result["markdown"] == "## Meeting summary · 09 Oct to 15 Oct 2026\n"
    assert meeting.short_reference("group/app#12") == "#12"
    assert meeting.short_reference("group&3") == "&3"
    assert meeting.short_reference("odd") == "odd"
    assert meeting._normalize('Draft: Resolve "Export CSV"') == "export csv"
    assert meeting._normalize("WIP: Closes 'A  b'") == "a b"
    assert meeting._short("x" * 100).endswith("…")
    assert meeting._duration(59) == "1m"
    assert meeting._age_days(None, NOW) == 0
    assert meeting._is_backlog("Icebox 2027")
    assert not meeting._is_backlog("2026.10")
    assert date(2026, 10, 15) == TODAY


# ---------------------------------------------------------------------- API


@pytest.fixture
def client(settings: Settings) -> Iterator[tuple[TestClient, AppContext]]:
    app = create_app(settings.model_copy(update={"scheduler_enabled": False}))
    with TestClient(app) as found:
        context: AppContext = app.state.ctx
        context.service.clock = lambda: NOW
        with context.session_factory() as session:
            user(session, 2, "alex", "Alex")
            user(session, 3, "kim", "Kim")
            user(session, 4, "sam", "Sam", selected=False)
            session.commit()
        yield found, context


def test_api_defaults_to_the_selection_and_keeps_the_requested_order(
    client: tuple[TestClient, AppContext],
) -> None:
    api, _ = client
    default = api.get("/api/meeting-summary")
    assert default.status_code == 200
    assert [p["user"]["username"] for p in default.json()["people"]] == ["alex", "kim"]
    assert default.json()["period"]["days"] == 7
    chosen = api.get("/api/meeting-summary", params={"user_ids": "4,2,4", "days": 30}).json()
    assert [p["user"]["username"] for p in chosen["people"]] == ["sam", "alex"]
    assert chosen["period"]["days"] == 30
    assert api.get("/api/meeting-summary", params={"user_ids": ""}).json()["people"] == []


@pytest.mark.parametrize(
    ("params", "status", "detail"),
    [
        ({"user_ids": "2,x"}, 422, "user_ids must be numbers"),
        ({"user_ids": ",".join(str(n) for n in range(51))}, 422, "At most 50 people"),
        ({"user_ids": "2,99"}, 404, "Unknown users: 99"),
        ({"days": 0}, 422, None),
        ({"days": 32}, 422, None),
    ],
)
def test_api_rejects_bad_input(
    client: tuple[TestClient, AppContext], params: dict[str, Any], status: int, detail: str | None
) -> None:
    api, _ = client
    response = api.get("/api/meeting-summary", params=params)
    assert response.status_code == status
    if detail:
        assert detail in response.json()["detail"]


def test_api_never_wakes_the_scheduler(
    client: tuple[TestClient, AppContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    api, context = client
    calls: list[str] = []
    monkeypatch.setattr(context.scheduler, "request_refresh", lambda: calls.append("refresh"))
    monkeypatch.setattr(context.scheduler, "request_user_sync", lambda uid: calls.append("user"))
    assert api.get("/api/meeting-summary").status_code == 200
    assert calls == []

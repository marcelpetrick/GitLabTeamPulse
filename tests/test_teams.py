"""Teams: storage and API, the report (periods, scope, deltas, spread) and the weekly digest."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from gitlab_team_pulse import store, teams
from gitlab_team_pulse.app import AppContext, create_app
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.gitlab.models import GitLabProject, GitLabUser
from gitlab_team_pulse.models import ActivityDaily, Outcome, TimelogDaily

from .conftest import Clock, fake_client_factory

ALEX, SAM, ROBIN = 2, 3, 4
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)  # a Thursday


@pytest.fixture
def api(
    settings: Settings, fake_app: FastAPI, clock: Clock
) -> Iterator[tuple[TestClient, AppContext]]:
    app = create_app(
        settings.model_copy(update={"scheduler_enabled": False}),
        client_factory=fake_client_factory(fake_app),
    )
    with TestClient(app) as client:
        context: AppContext = app.state.ctx
        context.service.clock = clock
        assert client.portal is not None
        client.portal.call(context.service.sync_directory, "startup")
        yield client, context


def create(client: TestClient, **body: object) -> dict[str, object]:
    payload = {"name": "Platform", "member_ids": [ALEX, SAM], "scopes": ["platform"], **body}
    response = client.post("/api/teams", json=payload)
    assert response.status_code == 201, response.text
    result: dict[str, object] = response.json()
    return result


# ---------------------------------------------------------------------- API


def test_team_crud_bumps_versions_and_requests_member_sync(
    api: tuple[TestClient, AppContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, context = api
    requested: list[int] = []
    monkeypatch.setattr(context.scheduler, "request_user_sync", requested.append)
    before = client.get("/api/status").json()["teams_version"]
    team = create(client, description="  API and web  ", scopes=["/platform/", "platform"])
    assert team["name"] == "Platform"
    assert team["description"] == "API and web"
    assert team["scopes"] == ["platform"]
    assert [m["id"] for m in team["members"]] == [ALEX, SAM]  # type: ignore[index]
    assert requested == [ALEX, SAM]
    assert client.get("/api/status").json()["teams_version"] == before + 1

    requested.clear()
    updated = client.put(
        f"/api/teams/{team['id']}",
        json={"name": "Platform team", "member_ids": [SAM, ROBIN], "scopes": []},
    ).json()
    assert updated["name"] == "Platform team"
    assert [m["id"] for m in updated["members"]] == [ROBIN, SAM]  # alphabetical by name
    assert requested == [ROBIN]  # only the new member needs a history sync
    listing = client.get("/api/teams").json()
    assert listing["readonly"] is False
    assert [t["name"] for t in listing["teams"]] == ["Platform team"]

    assert client.delete(f"/api/teams/{team['id']}").status_code == 204
    assert client.get("/api/teams").json()["teams"] == []
    assert client.delete(f"/api/teams/{team['id']}").status_code == 404
    assert client.put(f"/api/teams/{team['id']}", json={"name": "x"}).status_code == 404


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"name": "   "}, 400, "needs a name"),
        ({"name": "x" * 121}, 400, "longer than 120"),
        ({"name": "Other", "member_ids": [999]}, 400, "Unknown users: 999"),
        ({"name": "Other", "scopes": [f"g{i}" for i in range(51)]}, 400, "at most 50"),
        ({"name": "Other", "scopes": ["g/" + "p" * 520]}, 400, "longer than 512"),
        ({"name": "Platform"}, 409, "already exists"),
    ],
)
def test_team_validation(
    api: tuple[TestClient, AppContext], body: dict[str, object], status: int, detail: str
) -> None:
    client, _ = api
    create(client)
    response = client.post("/api/teams", json=body)
    assert response.status_code == status
    assert detail in response.json()["detail"]


def test_renaming_a_team_to_its_own_name_is_allowed(api: tuple[TestClient, AppContext]) -> None:
    client, _ = api
    team = create(client)
    response = client.put(f"/api/teams/{team['id']}", json={"name": "Platform"})
    assert response.status_code == 200


def test_readonly_teams_reject_changes(settings: Settings, fake_app: FastAPI, clock: Clock) -> None:
    app = create_app(
        settings.model_copy(update={"scheduler_enabled": False, "teams_readonly": True}),
        client_factory=fake_client_factory(fake_app),
    )
    with TestClient(app) as client:
        assert client.post("/api/teams", json={"name": "x"}).status_code == 403
        assert client.put("/api/teams/1", json={"name": "x"}).status_code == 403
        assert client.delete("/api/teams/1").status_code == 403
        assert client.get("/api/teams").json()["readonly"] is True
        assert client.get("/api/status").json()["teams"]["readonly"] is True


def test_apply_replaces_the_selection(
    api: tuple[TestClient, AppContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, context = api
    requested: list[int] = []
    monkeypatch.setattr(context.scheduler, "request_user_sync", requested.append)
    client.patch(f"/api/users/{ROBIN}/selection", json={"selected": True})
    team = create(client)
    requested.clear()
    result = client.post(f"/api/teams/{team['id']}/apply").json()
    assert result == {"selected": 2, "added": 2, "removed": 1}
    assert requested == [ALEX, SAM]
    selected = {u["id"] for u in client.get("/api/users").json()["users"] if u["selected"]}
    assert selected == {ALEX, SAM}
    assert client.post("/api/teams/999/apply").status_code == 404


def test_unknown_team_report_and_digest(api: tuple[TestClient, AppContext]) -> None:
    client, _ = api
    assert client.get("/api/teams/9/report").status_code == 404
    assert client.get("/api/teams/9/digest").status_code == 404
    assert client.get("/api/teams/9/report?period=year").status_code == 422


def test_report_after_a_real_sync_covers_history(api: tuple[TestClient, AppContext]) -> None:
    client, context = api
    team = create(client, scopes=[])
    assert client.portal is not None
    client.portal.call(context.service.sync_selected, "manual")
    report = client.get(f"/api/teams/{team['id']}/report?period=quarter&offset=1").json()
    assert report["history"]["complete"] is True
    assert report["totals"]["seconds"] > 0
    assert report["totals"]["mrs_merged"] + report["totals"]["issues_closed"] > 0
    assert report["projects"]
    assert report["people"] is None
    assert report["person_breakdown"] is False
    digest = client.get(f"/api/teams/{team['id']}/digest").json()
    assert digest["markdown"].startswith("## Platform")
    projects = client.get("/api/projects").json()
    assert "platform/api" in projects["projects"]
    assert "platform" in projects["namespaces"]


# ---------------------------------------------------------------------- report


def seed(session: Session, settings: Settings) -> int:
    for uid, name in ((ALEX, "Alex Berger"), (SAM, "Sam Novak"), (ROBIN, "Robin Silva")):
        store.upsert_users(
            session, [GitLabUser(id=uid, username=name.split()[0].lower(), name=name)], NOW
        )
    for pid, path in ((1, "platform/api"), (2, "platform/web"), (3, "tools/cli")):
        store.upsert_project(
            session, GitLabProject(id=pid, name=path, path_with_namespace=path), NOW
        )
    team = store.save_team(
        session,
        None,
        name="Platform",
        description="",
        member_ids=[ALEX, SAM],
        scopes=["platform"],
        now=NOW,
    )
    october, september = date(2026, 10, 2), date(2026, 9, 2)
    session.add_all(
        [
            TimelogDaily(user_id=ALEX, day=october, project_id=1, project_path="x", seconds=7200),
            TimelogDaily(user_id=SAM, day=october, project_id=2, project_path="x", seconds=3600),
            TimelogDaily(user_id=SAM, day=october, project_id=3, project_path="x", seconds=9000),
            TimelogDaily(user_id=ALEX, day=september, project_id=1, project_path="x", seconds=3600),
            # After the elapsed span of the running month: not part of the comparison.
            TimelogDaily(
                user_id=ALEX, day=date(2026, 9, 20), project_id=1, project_path="x", seconds=999
            ),
            TimelogDaily(user_id=ROBIN, day=october, project_id=1, project_path="x", seconds=50),
            ActivityDaily(user_id=ALEX, day=october, project_id=1, metric="mr_approved", value=2),
            ActivityDaily(user_id=SAM, day=october, project_id=2, metric="mr_commented", value=3),
            ActivityDaily(user_id=SAM, day=october, project_id=1, metric="mr_opened", value=1),
            ActivityDaily(user_id=SAM, day=october, project_id=1, metric="issue_closed", value=4),
        ]
    )
    merged = datetime(2026, 10, 5, 10, tzinfo=UTC)
    for uid in (ALEX, SAM):  # the same MR seen through two members counts once
        session.add(
            Outcome(
                user_id=uid,
                kind="merge_request",
                gitlab_id=77,
                project_id=1,
                iid=7,
                reference="platform/api!7",
                title="Faster builds",
                web_url=f"{settings.gitlab_url}/platform/api/-/merge_requests/7",
                created_at=merged - timedelta(hours=10),
                finished_at=merged,
            )
        )
    session.add_all(
        [
            Outcome(
                user_id=SAM,
                kind="issue",
                gitlab_id=88,
                project_id=2,
                iid=8,
                reference="platform/web#8",
                title="Broken link",
                web_url="https://elsewhere.example/8",
                created_at=None,
                finished_at=datetime(2026, 10, 6, 9, tzinfo=UTC),
                time_estimate=3600,
                time_spent=5400,
            ),
            Outcome(
                user_id=SAM,
                kind="issue",
                gitlab_id=89,
                project_id=3,
                iid=9,
                reference="tools/cli#9",
                title="Out of scope",
                web_url=None,
                created_at=None,
                finished_at=datetime(2026, 10, 6, 9, tzinfo=UTC),
            ),
        ]
    )
    session.commit()
    return team.id


def test_report_totals_scope_deltas_and_projects(
    session_factory: object, settings: Settings
) -> None:
    with session_factory() as session:  # type: ignore[operator]
        team_id = seed(session, settings)
        report = teams.TeamReporter(session, settings, NOW).report(team_id, "month", 0)
    assert report is not None
    period = report["period"]
    assert (period["label"], period["running"], period["elapsed_days"]) == (
        "October 2026",
        True,
        15,
    )
    assert period["previous"]["start"] == date(2026, 9, 1)
    assert period["previous"]["end"] == date(2026, 9, 15)
    assert period["previous"]["partial"]
    totals = report["totals"]
    assert totals["seconds"] == 7200 + 3600  # tools/cli and non-members are out of scope
    assert (totals["mrs_merged"], totals["issues_closed"]) == (1, 1)
    assert (totals["approvals"], totals["review_comments"], totals["mrs_opened"]) == (2, 3, 1)
    assert totals["cycle_time_hours"] == {"median": 10.0, "p25": 10.0, "p75": 10.0, "count": 1}
    assert totals["estimate"] == {
        "issues": 1,
        "with_estimate": 1,
        "estimate_seconds": 3600,
        "spent_seconds": 5400,
    }
    assert report["previous"]["seconds"] == 3600  # same 15 days of September only
    assert [(p["path"], p["seconds"], p["share"]) for p in report["projects"]] == [
        ("platform/api", 7200, 0.6667),
        ("platform/web", 3600, 0.3333),
    ]
    spread = {row["path"]: row for row in report["spread"]}
    assert spread["platform/api"]["count"] == 2
    assert spread["platform/web"] == {
        "path": "platform/web",
        "people": ["Sam Novak"],
        "count": 1,
        "single": True,
    }
    assert report["history"]["complete"] is False
    assert report["history"]["pending"] == ["Alex Berger", "Sam Novak"]


def test_person_breakdown_is_alphabetical_facts(
    session_factory: object, settings: Settings
) -> None:
    settings = settings.model_copy(update={"team_person_breakdown": True})
    with session_factory() as session:  # type: ignore[operator]
        team_id = seed(session, settings)
        report = teams.TeamReporter(session, settings, NOW).report(team_id, "month", 0)
    assert report is not None
    people = report["people"]
    assert [p["name"] for p in people] == ["Alex Berger", "Sam Novak"]
    alex, sam = people
    assert (alex["seconds"], alex["days_with_time"], alex["approvals"]) == (7200, 1, 2)
    assert (sam["mrs_merged"], sam["issues_closed"], sam["review_comments"]) == (1, 1, 3)


def test_history_completeness_and_unknown_team(session_factory: object, settings: Settings) -> None:
    with session_factory() as session:  # type: ignore[operator]
        team_id = seed(session, settings)
        for uid in (ALEX, SAM):
            for category in ("contributions", "timelog_history", "outcomes"):
                store.mark_success(store.get_state(session, category, uid), NOW)
        session.commit()
        reporter = teams.TeamReporter(session, settings, NOW)
        report = reporter.report(team_id, "week", 53)
        assert reporter.report(999, "week", 0) is None
    assert report is not None
    assert report["history"] == {
        "from": date(2025, 10, 12),
        "complete": True,
        "pending": [],
        "updated_at": NOW,
    }
    assert report["period"]["before_history"] is True
    assert report["period"]["running"] is False
    assert report["totals"]["cycle_time_hours"] is None


def test_digest_lists_last_week(session_factory: object, settings: Settings) -> None:
    now = datetime(2026, 10, 13, 9, tzinfo=UTC)  # Tuesday after the seeded week
    with session_factory() as session:  # type: ignore[operator]
        team_id = seed(session, settings)
        result = teams.digest(session, settings, now, team_id)
        assert teams.digest(session, settings, now, 999) is None
    assert result is not None
    markdown = result["markdown"]
    assert result["period"]["label"] == "Week 41, 2026 (05 Oct to 11 Oct)"
    assert "## Platform — Week 41" in markdown
    assert "Merge requests merged: **1**" in markdown
    link = f"[platform/api!7]({settings.gitlab_url}/platform/api/-/merge_requests/7)"
    assert f"- {link} Faster builds (Alex Berger)" in markdown
    assert "- platform/web#8 Broken link (Sam Novak)" in markdown  # foreign link not passed on
    assert "Out of scope" not in markdown
    assert "Scope: platform" in markdown
    assert "History is still loading for: Alex Berger, Sam Novak" in markdown


# ---------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("period", "offset", "start", "end", "label"),
    [
        ("week", 0, date(2026, 10, 12), date(2026, 10, 19), "Week 42, 2026 (12 Oct to 18 Oct)"),
        ("week", 2, date(2026, 9, 28), date(2026, 10, 5), "Week 40, 2026 (28 Sep to 04 Oct)"),
        ("month", 0, date(2026, 10, 1), date(2026, 11, 1), "October 2026"),
        ("month", 10, date(2025, 12, 1), date(2026, 1, 1), "December 2025"),
        ("quarter", 0, date(2026, 10, 1), date(2027, 1, 1), "Q4 2026"),
        ("quarter", 4, date(2025, 10, 1), date(2026, 1, 1), "Q4 2025"),
    ],
)
def test_periods(period: teams.Period, offset: int, start: date, end: date, label: str) -> None:
    today = date(2026, 10, 15)
    assert teams.period_start(period, today, offset) == start
    assert teams.period_end(period, start) == end
    assert teams.period_label(period, start) == label


def test_scope_matching() -> None:
    assert teams.in_scope("anything", [])
    assert teams.in_scope(None, [])
    assert not teams.in_scope(None, ["platform"])
    assert teams.in_scope("platform/api", ["platform"])
    assert teams.in_scope("platform", ["platform"])
    assert not teams.in_scope("platformer/api", ["platform"])
    assert teams.normalize_scope(" /a/b/ ") == "a/b"


def test_quartiles() -> None:
    assert teams._quartiles([]) is None
    assert teams._quartiles([1.0, 2.0, 3.0, 4.0, 5.0]) == {
        "median": 3.0,
        "p25": 2.0,
        "p75": 4.0,
        "count": 5,
    }
    assert teams._duration(5400) == "1h 30m"
    assert teams._duration(600) == "10m"

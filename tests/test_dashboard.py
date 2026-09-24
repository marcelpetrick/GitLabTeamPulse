from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import dashboard, store
from gitlab_team_pulse.app import create_app
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.models import (
    ActivityEventRecord,
    TimelogRecord,
    User,
    WorkItemAssignee,
    WorkItemRecord,
)
from gitlab_team_pulse.sync import RuntimeState

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://fake.gitlab/grp/app", "http://fake.gitlab/grp/app"),
        ("http://fake.gitlab", "http://fake.gitlab"),
        ("/grp/app/-/issues/1", "http://fake.gitlab/grp/app/-/issues/1"),
        ("//evil.test/x", None),
        ("http://fake.gitlab.evil.test/x", None),
        ("javascript:alert(1)", None),
        ("https://elsewhere.test/", None),
        (None, None),
        ("", None),
    ],
)
def test_safe_url(settings: Settings, url: str | None, expected: str | None) -> None:
    assert dashboard.safe_url(url, settings) == expected


def test_safe_url_without_configured_base(settings: Settings) -> None:
    assert dashboard.safe_url("http://x/y", settings.model_copy(update={"gitlab_url": ""})) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://secure.gravatar.com/avatar/1", "https://secure.gravatar.com/avatar/1"),
        ("/uploads/a.png", "http://fake.gitlab/uploads/a.png"),
        ("data:image/png;base64,xx", None),
        (None, None),
    ],
)
def test_safe_avatar(settings: Settings, url: str | None, expected: str | None) -> None:
    assert dashboard.safe_avatar(url, settings) == expected


def seed(session: Session) -> None:
    session.add(
        User(id=1, username="kim", name="Kim", selected=True, first_seen_at=NOW, last_seen_at=NOW)
    )
    session.flush()
    item = WorkItemRecord(
        kind="merge_request",
        gitlab_id=5,
        iid=3,
        project_id=77,  # never resolved
        reference="!3",
        title="Review me",
        state="opened",
        labels=[],
        assignees=["kim"],
        updated_at=NOW,
        refreshed_at=NOW,
        web_url="https://elsewhere.test/x",
    )
    session.add(item)
    session.flush()
    for relation in ("assignee", "reviewer"):
        session.add(
            WorkItemAssignee(work_item_id=item.id, user_id=1, relation=relation, observed_at=NOW)
        )
    session.add_all(
        [
            ActivityEventRecord(
                id=1,
                user_id=1,
                project_id=None,
                category="mystery",
                action_name="joined",
                summary="Joined",
                occurred_at=NOW,
                url_path=None,
                fetched_at=NOW,
            ),
            ActivityEventRecord(
                id=2,
                user_id=1,
                project_id=77,
                category="push",
                action_name="pushed to",
                summary="Pushed",
                occurred_at=NOW - timedelta(days=30),
                url_path="/-/commits/main",
                fetched_at=NOW,
            ),
            TimelogRecord(id=1, user_id=1, spent_at=NOW, seconds=600, fetched_at=NOW),
            TimelogRecord(
                id=2, user_id=1, spent_at=NOW - timedelta(days=20), seconds=60, fetched_at=NOW
            ),
        ]
    )
    session.commit()


def test_reader_edge_cases(session_factory: sessionmaker[Session], settings: Settings) -> None:
    with session_factory() as session:
        seed(session)
        reader = dashboard.DashboardReader(session, settings, RuntimeState(), NOW)
        card = reader.cards(store.selected_users(session))[0]
    work = card["work"][0]
    assert work["relations"] == ["assignee", "reviewer"]
    assert work["web_url"] is None  # not the configured GitLab host
    assert work["project"] == {"id": 77, "name": "Project #77", "path": None, "web_url": None}
    assert card["counts"]["work_open"] == 1
    assert card["activity"][0]["project"] is None
    assert card["activity"][0]["web_url"] is None
    assert card["activity"][1]["web_url"] is None  # project metadata missing
    today = card["activity_days"][-1]
    assert today["other"] == 1  # unknown categories are counted as "other"
    assert card["time"]["total_seconds"] == 600  # entries outside the window are ignored
    assert card["time"]["projects"] == [{"path": "Unknown project", "seconds": 600}]
    assert card["freshness"]["status"] == "never"
    assert reader.cards([]) == []


def test_user_freshness_combines_datasets(settings: Settings) -> None:
    states = {}
    for dataset, success, status in [
        ("work", NOW - timedelta(minutes=1), "ok"),
        ("activity", NOW - timedelta(minutes=3), "ok"),
        ("timelogs", NOW - timedelta(minutes=2), "error"),
    ]:
        state = store.SyncState(
            key=store.state_key(dataset, 1),
            category=dataset,
            user_id=1,
            status=status,
            last_success_at=success,
            last_attempt_at=NOW,
            last_error="boom" if status == "error" else None,
        )
        states[state.key] = state
    info = dashboard.user_freshness(states, 1, RuntimeState(), settings, NOW)
    assert info["status"] == "error"
    assert info["last_success_at"] == NOW - timedelta(minutes=3)
    assert info["message"] == "timelogs: boom"
    running = dashboard.user_freshness(states, 1, RuntimeState(running={"selected"}), settings, NOW)
    assert running["status"] == "refreshing"
    other_user_run = RuntimeState(running={"selected"}, selected_scope=frozenset({2}))
    assert dashboard.user_freshness(states, 1, other_user_run, settings, NOW)["status"] == "error"
    own_run = RuntimeState(running={"selected"}, selected_scope=frozenset({1}))
    assert dashboard.user_freshness(states, 1, own_run, settings, NOW)["status"] == "refreshing"


def test_local_days_follow_timezone(settings: Settings) -> None:
    berlin = settings.model_copy(update={"timezone": "Pacific/Kiritimati"})
    days = dashboard.local_days(berlin, datetime(2026, 9, 24, 23, 0, tzinfo=UTC))
    assert len(days) == 7
    assert days[-1].isoformat() == "2026-09-25"


def test_unconfigured_app_starts_and_warns(settings: Settings) -> None:
    config = settings.model_copy(update={"gitlab_url": "", "scheduler_enabled": True})
    with TestClient(create_app(config)) as client:
        assert client.get("/api/health").json()["gitlab_configured"] is False
        assert client.get("/api/status").json()["gitlab"]["configured"] is False

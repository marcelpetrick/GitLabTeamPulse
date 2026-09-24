from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gitlab_team_pulse.freshness import Freshness, classify

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def run(**kwargs: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = {
        "last_success_at": NOW - timedelta(minutes=5),
        "last_attempt_at": NOW - timedelta(minutes=5),
        "last_status": "ok",
        "running": False,
        "interval_seconds": 600,
        "grace_seconds": 120,
        "now": NOW,
    }
    base.update(kwargs)
    return classify(**base)  # type: ignore[arg-type]


def test_fresh() -> None:
    info = run()
    assert info.status is Freshness.FRESH
    assert info.age_seconds == 300
    assert not info.stale
    assert not info.error


def test_stale_after_interval_plus_grace() -> None:
    assert run(last_success_at=NOW - timedelta(seconds=720)).status is Freshness.FRESH
    info = run(last_success_at=NOW - timedelta(seconds=721))
    assert info.status is Freshness.STALE
    assert info.stale


def test_never_synced() -> None:
    info = run(last_success_at=None, last_status="never")
    assert info.status is Freshness.NEVER
    assert info.age_seconds is None
    assert info.stale is False


@pytest.mark.parametrize("status", ["error", "partial"])
def test_error_keeps_stale_flag(status: str) -> None:
    info = run(last_success_at=NOW - timedelta(days=2), last_status=status, message="boom")
    assert info.status is Freshness.ERROR
    assert info.stale
    assert info.error
    assert info.message == "boom"


def test_message_hidden_when_not_error() -> None:
    assert run(message="old").message is None


def test_refreshing_wins_and_keeps_flags() -> None:
    info = run(running=True, last_status="error", last_success_at=NOW - timedelta(hours=1))
    assert info.status is Freshness.REFRESHING
    assert info.stale
    assert info.error
    assert info.as_dict()["status"] == "refreshing"


def test_never_with_error() -> None:
    info = run(last_success_at=None, last_status="error")
    assert info.status is Freshness.NEVER
    assert info.error

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import store

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def test_data_version_increments(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        assert store.data_version(session) == 0
        assert store.bump_data_version(session) == 1
        assert store.bump_data_version(session) == 2
        session.commit()
    with session_factory() as session:
        assert store.data_version(session) == 2
        assert store.get_value(session, "missing") is None


def test_sync_state_lifecycle(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        state = store.get_state(session, "directory")
        assert state.status == "never"
        store.mark_attempt(state, NOW, "run-1")
        store.mark_success(state, NOW, cursor="c", next_due_at=NOW + timedelta(hours=1))
        session.commit()
    with session_factory() as session:
        state = store.get_state(session, "directory")
        assert state.status == "ok"
        assert state.cursor == "c"
        store.mark_failure(state, "x" * 5000)
        session.commit()
        assert state.last_success_at == NOW
        assert state.last_error is not None
        assert len(state.last_error) == store.MAX_MESSAGE
        assert store.find_state(session, "activity", 5) is None
        assert store.state_key("activity", 5) == "activity:5"


def test_errors_are_deduplicated_and_resolved(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        first = store.record_error(
            session, subsystem="sync", operation="users", message="down", now=NOW
        )
        session.flush()
        again = store.record_error(
            session,
            subsystem="sync",
            operation="users",
            message="down",
            now=NOW + timedelta(minutes=1),
        )
        assert first is again
        assert again.occurrences == 2
        assert again.first_occurred_at == NOW
        store.record_error(
            session, subsystem="sync", operation="work", message="down", now=NOW, user_id=3
        )
        store.record_error(
            session, subsystem="sync", operation="work", message="down", now=NOW, user_id=4
        )
        session.flush()
        assert store.unresolved_error_count(session) == 3
        assert (
            store.resolve_errors(session, subsystem="sync", operation="work", user_id=3, now=NOW)
            == 1
        )
        assert store.unresolved_error_count(session) == 2
        assert len(store.list_errors(session, include_resolved=False)) == 2
        assert len(store.list_errors(session)) == 3
        assert store.resolve_errors(session, subsystem="sync", now=NOW) == 2
        session.commit()
        assert store.unresolved_error_count(session) == 0


def test_runs(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        assert store.latest_run(session, "selected") is None
        run = store.start_run(session, kind="selected", trigger="manual", now=NOW)
        later = store.start_run(
            session, kind="selected", trigger="scheduled", now=NOW + timedelta(minutes=1)
        )
        store.finish_run(run, status="ok", now=NOW, summary="done")
        session.commit()
        latest = store.latest_run(session, "selected")
        assert latest is not None
        assert latest.id == later.id
        assert run.finished_at == NOW

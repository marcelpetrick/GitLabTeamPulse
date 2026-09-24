from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse.db import (
    current_revision,
    downgrade,
    head_revision,
    is_migrated,
    make_engine,
    migrate,
    ping,
)
from gitlab_team_pulse.models import Base, User


def test_migrate_from_empty_database_reaches_head(engine: Engine) -> None:
    assert current_revision(engine) == head_revision()
    assert is_migrated(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"users", "sync_state", "sync_runs", "errors", "app_state"} <= tables


def test_models_and_migrations_do_not_drift(engine: Engine) -> None:
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_downgrade_to_base_and_back(engine: Engine) -> None:
    downgrade(engine, "base")
    assert current_revision(engine) is None
    assert not is_migrated(engine)
    migrate(engine)
    assert is_migrated(engine)


def test_connection_pragmas(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_database_directory_is_created(tmp_path: Path) -> None:
    eng = make_engine(f"sqlite:///{tmp_path / 'nested' / 'dir' / 'x.db'}")
    assert ping(eng)
    assert (tmp_path / "nested" / "dir").is_dir()
    eng.dispose()


def test_ping_reports_failure(tmp_path: Path) -> None:
    eng = make_engine(f"sqlite:///{tmp_path / 'x.db'}")
    (tmp_path / "x.db").mkdir()  # a directory cannot be opened as a database
    assert ping(eng) is False


def test_utc_datetime_roundtrip(session_factory: sessionmaker[Session]) -> None:
    berlin = timezone(timedelta(hours=2))
    stamp = datetime(2026, 9, 24, 12, 0, tzinfo=berlin)
    with session_factory() as session:
        session.add(User(id=1, username="a", first_seen_at=stamp, last_seen_at=stamp))
        session.commit()
    with session_factory() as session:
        user = session.get(User, 1)
        assert user is not None
        assert user.first_seen_at == datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
        assert user.first_seen_at.tzinfo is UTC
        assert user.created_at is None


def test_naive_datetime_is_rejected(session_factory: sessionmaker[Session]) -> None:
    naive = datetime(2026, 9, 24, 12, 0)  # noqa: DTZ001
    with session_factory() as session:
        session.add(User(id=2, username="b", first_seen_at=naive, last_seen_at=naive))
        with pytest.raises(StatementError):
            session.commit()

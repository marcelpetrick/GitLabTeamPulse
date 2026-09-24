"""SQLite engine, sessions and Alembic migrations (bundled inside the package)."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def make_engine(database_url: str) -> Engine:
    """Create an engine with foreign keys, WAL and a busy timeout enabled on every connection."""
    if database_url.startswith("sqlite:///") and ":memory:" not in database_url:
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def alembic_config(engine: Engine) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    config.attributes["engine"] = engine
    return config


def migrate(engine: Engine, revision: str = "head") -> None:
    """Upgrade the schema; never requires deleting the database."""
    command.upgrade(alembic_config(engine), revision)


def downgrade(engine: Engine, revision: str) -> None:
    command.downgrade(alembic_config(engine), revision)


@cache
def head_revision() -> str | None:
    """The newest shipped revision; migration scripts never change at runtime, so parse once
    instead of on every /api/health call."""
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    return script.get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def is_migrated(engine: Engine) -> bool:
    return current_revision(engine) == head_revision()


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True

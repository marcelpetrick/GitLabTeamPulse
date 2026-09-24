"""Alembic environment; the engine is handed over via ``config.attributes``."""

from __future__ import annotations

from alembic import context

from gitlab_team_pulse.models import Base

config = context.config
engine = config.attributes["engine"]

with engine.connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
    connection.commit()

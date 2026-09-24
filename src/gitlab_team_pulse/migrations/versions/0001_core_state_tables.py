"""core state tables

Revision ID: 0001
Revises:
Create Date: 2026-09-24 09:27:02.468819
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_state",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_state")),
    )
    op.create_table(
        "errors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("first_occurred_at", sa.DateTime(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("subsystem", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_errors")),
    )
    with op.batch_alter_table("errors", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_errors_occurred_at"), ["occurred_at"], unique=False)
        batch_op.create_index(
            "ix_errors_open_key", ["subsystem", "operation", "user_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_errors_resolved_at"), ["resolved_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_errors_user_id"), ["user_id"], unique=False)

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("users_attempted", sa.Integer(), nullable=False),
        sa.Column("users_succeeded", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    with op.batch_alter_table("sync_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sync_runs_started_at"), ["started_at"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("web_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("selected_at", sa.DateTime(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_users_selected"), ["selected"], unique=False)
        batch_op.create_index(batch_op.f("ix_users_username"), ["username"], unique=False)

    op.create_table(
        "sync_state",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("next_due_at", sa.DateTime(), nullable=True),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sync_state_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_sync_state")),
    )
    with op.batch_alter_table("sync_state", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sync_state_category"), ["category"], unique=False)
        batch_op.create_index(batch_op.f("ix_sync_state_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("sync_state", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_state_user_id"))
        batch_op.drop_index(batch_op.f("ix_sync_state_category"))

    op.drop_table("sync_state")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_username"))
        batch_op.drop_index(batch_op.f("ix_users_selected"))

    op.drop_table("users")
    with op.batch_alter_table("sync_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_runs_started_at"))

    op.drop_table("sync_runs")
    with op.batch_alter_table("errors", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_errors_user_id"))
        batch_op.drop_index(batch_op.f("ix_errors_resolved_at"))
        batch_op.drop_index("ix_errors_open_key")
        batch_op.drop_index(batch_op.f("ix_errors_occurred_at"))

    op.drop_table("errors")
    op.drop_table("app_state")

"""work, activity, timelog and project cache

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24 09:36:42.338355
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("path_with_namespace", sa.String(length=512), nullable=False),
        sa.Column("web_url", sa.Text(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_projects_refreshed_at"), ["refreshed_at"], unique=False
        )

    op.create_table(
        "work_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("gitlab_id", sa.Integer(), nullable=False),
        sa.Column("iid", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("reference", sa.String(length=512), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("issue_type", sa.String(length=32), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("milestone", sa.String(length=255), nullable=True),
        sa.Column("priority", sa.String(length=64), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("web_url", sa.Text(), nullable=True),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("assignees", sa.JSON(), nullable=False),
        sa.Column("draft", sa.Boolean(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_items")),
        sa.UniqueConstraint("kind", "gitlab_id", name="uq_work_items_kind_gitlab_id"),
    )
    with op.batch_alter_table("work_items", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_work_items_project_id"), ["project_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_work_items_state"), ["state"], unique=False)
        batch_op.create_index(batch_op.f("ix_work_items_updated_at"), ["updated_at"], unique=False)

    op.create_table(
        "activity_events",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("action_name", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("url_path", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_activity_events_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activity_events")),
    )
    with op.batch_alter_table("activity_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_activity_events_project_id"), ["project_id"], unique=False
        )
        batch_op.create_index(
            "ix_activity_events_user_time", ["user_id", "occurred_at"], unique=False
        )

    op.create_table(
        "timelogs",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("project_path", sa.String(length=512), nullable=True),
        sa.Column("spent_at", sa.DateTime(), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("target_kind", sa.String(length=16), nullable=True),
        sa.Column("target_iid", sa.Integer(), nullable=True),
        sa.Column("target_title", sa.Text(), nullable=True),
        sa.Column("web_url", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_timelogs_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_timelogs")),
    )
    with op.batch_alter_table("timelogs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_timelogs_project_id"), ["project_id"], unique=False)
        batch_op.create_index("ix_timelogs_user_time", ["user_id", "spent_at"], unique=False)

    op.create_table(
        "work_item_assignees",
        sa.Column("work_item_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("relation", sa.String(length=16), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_work_item_assignees_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_items.id"],
            name=op.f("fk_work_item_assignees_work_item_id_work_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "work_item_id", "user_id", "relation", name=op.f("pk_work_item_assignees")
        ),
    )
    with op.batch_alter_table("work_item_assignees", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_work_item_assignees_user_id"), ["user_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("work_item_assignees", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_work_item_assignees_user_id"))

    op.drop_table("work_item_assignees")
    with op.batch_alter_table("timelogs", schema=None) as batch_op:
        batch_op.drop_index("ix_timelogs_user_time")
        batch_op.drop_index(batch_op.f("ix_timelogs_project_id"))

    op.drop_table("timelogs")
    with op.batch_alter_table("activity_events", schema=None) as batch_op:
        batch_op.drop_index("ix_activity_events_user_time")
        batch_op.drop_index(batch_op.f("ix_activity_events_project_id"))

    op.drop_table("activity_events")
    with op.batch_alter_table("work_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_work_items_updated_at"))
        batch_op.drop_index(batch_op.f("ix_work_items_state"))
        batch_op.drop_index(batch_op.f("ix_work_items_project_id"))

    op.drop_table("work_items")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_projects_refreshed_at"))

    op.drop_table("projects")

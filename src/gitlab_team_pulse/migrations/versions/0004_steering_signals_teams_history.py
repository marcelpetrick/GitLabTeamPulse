"""steering signals, teams and history aggregates

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _user_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["user_id"], ["users.id"], name=op.f(f"fk_{table}_user_id_users"), ondelete="CASCADE"
    )


def upgrade() -> None:
    with op.batch_alter_table("work_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("merged_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("time_estimate", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("time_spent", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(sa.Column("reviewers", sa.JSON(), server_default="[]", nullable=False))
        batch_op.add_column(sa.Column("merge_status", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("has_conflicts", sa.Boolean(), server_default="0", nullable=False)
        )
        batch_op.add_column(sa.Column("discussions_resolved", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("milestone_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_work_items_milestone_id"), ["milestone_id"], unique=False
        )

    op.create_table(
        "milestones",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("iid", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("web_url", sa.Text(), nullable=True),
        sa.Column("stats", sa.JSON(), nullable=True),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.Column("stats_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_milestones")),
    )
    with op.batch_alter_table("milestones", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_milestones_seen_at"), ["seen_at"], unique=False)

    op.create_table(
        "teams",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
        sa.UniqueConstraint("name", name=op.f("uq_teams_name")),
    )
    op.create_table(
        "team_members",
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name=op.f("fk_team_members_team_id_teams"),
            ondelete="CASCADE",
        ),
        _user_fk("team_members"),
        sa.PrimaryKeyConstraint("team_id", "user_id", name=op.f("pk_team_members")),
    )
    with op.batch_alter_table("team_members", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_team_members_user_id"), ["user_id"], unique=False)
    op.create_table(
        "team_scopes",
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name=op.f("fk_team_scopes_team_id_teams"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("team_id", "path", name=op.f("pk_team_scopes")),
    )

    op.create_table(
        "activity_daily",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("project_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        _user_fk("activity_daily"),
        sa.PrimaryKeyConstraint(
            "user_id", "day", "project_id", "metric", name=op.f("pk_activity_daily")
        ),
    )
    op.create_table(
        "timelog_daily",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("project_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("project_path", sa.String(length=512), nullable=True),
        sa.Column("seconds", sa.Integer(), nullable=False),
        _user_fk("timelog_daily"),
        sa.PrimaryKeyConstraint("user_id", "day", "project_id", name=op.f("pk_timelog_daily")),
    )
    op.create_table(
        "outcomes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("gitlab_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("iid", sa.Integer(), nullable=False),
        sa.Column("reference", sa.String(length=512), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("web_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.Column("time_estimate", sa.Integer(), nullable=False),
        sa.Column("time_spent", sa.Integer(), nullable=False),
        _user_fk("outcomes"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outcomes")),
        sa.UniqueConstraint("user_id", "kind", "gitlab_id", name="uq_outcomes_user_kind_gitlab_id"),
    )
    with op.batch_alter_table("outcomes", schema=None) as batch_op:
        batch_op.create_index("ix_outcomes_user_finished", ["user_id", "finished_at"], unique=False)


def downgrade() -> None:
    op.drop_table("outcomes")
    op.drop_table("timelog_daily")
    op.drop_table("activity_daily")
    op.drop_table("team_scopes")
    op.drop_table("team_members")
    op.drop_table("teams")
    op.drop_table("milestones")
    with op.batch_alter_table("work_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_work_items_milestone_id"))
        batch_op.drop_column("milestone_id")
        batch_op.drop_column("discussions_resolved")
        batch_op.drop_column("has_conflicts")
        batch_op.drop_column("merge_status")
        batch_op.drop_column("reviewers")
        batch_op.drop_column("time_spent")
        batch_op.drop_column("time_estimate")
        batch_op.drop_column("merged_at")

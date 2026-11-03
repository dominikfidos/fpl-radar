"""Baseline: the tables the application already had before we used Alembic.

Databases that were created by the old `create_all` already contain these tables, so every
step first checks whether the table exists. Result: the same migration works on a brand-new
database AND adopts an existing one without touching its data.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _missing(name: str) -> bool:
    if context.is_offline_mode():  # `--sql` only prints SQL, there is no database to look at
        return True
    return not sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _missing("players"):
        op.create_table(
            "players",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("team", sa.String(50), nullable=False),
            sa.Column("position", sa.String(3), nullable=False),
        )
    if _missing("snapshots"):
        op.create_table(
            "snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
            sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("price", sa.Float(), nullable=False),
            sa.Column("form", sa.Float(), nullable=False),
            sa.Column("total_points", sa.Integer(), nullable=False),
            sa.Column("selected_by_percent", sa.Float(), nullable=False),
        )
        op.create_index("ix_snapshots_player_id", "snapshots", ["player_id"])
        op.create_index("ix_snapshots_taken_at", "snapshots", ["taken_at"])
    if _missing("api_cache"):
        op.create_table(
            "api_cache",
            sa.Column("key", sa.String(300), primary_key=True),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        )


def downgrade() -> None:
    # Deliberately empty: the baseline holds data that existed before migrations, so going
    # back past it must never delete it.
    pass

"""Add rank_history: a manager's place in a mini-league after each finished gameweek.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table("rank_history"):
        return
    op.create_table(
        "rank_history",
        sa.Column("league_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("entry_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("event", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("total_points", sa.Integer(), nullable=False),
        sa.Column("event_points", sa.Integer(), nullable=False),
        sa.Column("team", sa.String(100), nullable=True),
        sa.Column("manager", sa.String(100), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("league_id", "entry_id", "event"),
    )


def downgrade() -> None:
    op.drop_table("rank_history")

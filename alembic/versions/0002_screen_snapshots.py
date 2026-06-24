"""screening snapshots: screen_snapshots and screen_snapshot_items

Revision ID: 0002_screen_snapshots
Revises: 0001_initial
Create Date: 2026-06-24

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_screen_snapshots"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "screen_snapshots",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.Text(), server_default="", nullable=False),
        sa.Column("universe", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quotable", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pool_size", sa.Integer(), server_default="0", nullable=False),
        sa.Column("item_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("warning", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "trade_date", "session", name="uq_snapshot_date_session"
        ),
    )
    op.create_index(
        "ix_screen_snapshots_trade_date", "screen_snapshots", ["trade_date"]
    )

    op.create_table(
        "screen_snapshot_items",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), server_default="0", nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), server_default="", nullable=False),
        sa.Column("market", sa.Text(), server_default="", nullable=False),
        sa.Column("market_code", sa.Text(), server_default="", nullable=False),
        sa.Column("close", sa.Numeric(12, 4), nullable=True),
        sa.Column("prev_close", sa.Numeric(12, 4), nullable=True),
        sa.Column("change", sa.Numeric(12, 4), nullable=True),
        sa.Column("change_pct", sa.Numeric(12, 4), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("lots", sa.Numeric(16, 2), nullable=True),
        sa.Column("open", sa.Numeric(12, 4), nullable=True),
        sa.Column("high", sa.Numeric(12, 4), nullable=True),
        sa.Column("low", sa.Numeric(12, 4), nullable=True),
        sa.Column("prev_high", sa.Numeric(12, 4), nullable=True),
        sa.Column("vol_ratio", sa.Numeric(12, 4), nullable=True),
        sa.Column("ma5", sa.Numeric(12, 4), nullable=True),
        sa.Column("ma20", sa.Numeric(12, 4), nullable=True),
        sa.Column("ma20_up", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["screen_snapshots.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_screen_snapshot_items_snapshot_id",
        "screen_snapshot_items",
        ["snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_screen_snapshot_items_snapshot_id",
        table_name="screen_snapshot_items",
    )
    op.drop_table("screen_snapshot_items")
    op.drop_index(
        "ix_screen_snapshots_trade_date", table_name="screen_snapshots"
    )
    op.drop_table("screen_snapshots")

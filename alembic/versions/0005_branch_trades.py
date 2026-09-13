"""branch trades"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
revision: str = "0005_branch_trades"
down_revision: Union[str, None] = "0004_daily_prices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.create_table("branch_trades",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("branch_code", sa.String(16), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("branch_name", sa.Text(), server_default="", nullable=False),
        sa.Column("stock_name", sa.Text(), server_default="", nullable=False),
        sa.Column("buy_amount", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("sell_amount", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("net_amount", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("inventory_cost", sa.BigInteger(), nullable=True),
        sa.Column("inventory_value", sa.BigInteger(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("trade_date", "branch_code", "symbol"),
        sa.UniqueConstraint("trade_date", "branch_code", "symbol", name="uq_branch_trade"),
    )
    op.create_index("ix_branch_trades_symbol", "branch_trades", ["symbol"])

def downgrade() -> None:
    op.drop_index("ix_branch_trades_symbol", table_name="branch_trades")
    op.drop_table("branch_trades")

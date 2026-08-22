"""whole-market daily closes: daily_prices

Revision ID: 0004_daily_prices
Revises: 0003_email_tokens
Create Date: 2026-08-22

Backtesting asks "what was this stock worth N trading days after it was
screened?" — a question the snapshot tables cannot answer, because a stock only
appears there on the days it passed the filter. This table holds an ordinary
daily bar for the whole market, one row per (trade_date, symbol), uploaded by
the screener after close.

Design notes:
  * Composite natural primary key (trade_date, symbol). The table grows by
    ~1,800 rows per trading day and is read as "these symbols, on these dates",
    which the PK index serves directly. A surrogate UUID would cost storage and
    a second index for no benefit, and would let the same bar be inserted twice.
  * A secondary index on symbol alone, for the "one stock's whole series" reads
    (chart/detail views) that lead with the symbol.
  * close is NOT NULL: a row exists to state a price. The other OHLC columns are
    nullable because some sources omit them for thinly traded issues.
  * The distinct trade_date values double as the trading calendar the backtest
    counts against, so no holiday table is needed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_daily_prices"
down_revision: Union[str, None] = "0003_email_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_prices",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("name", sa.Text(), server_default="", nullable=False),
        sa.Column("market_code", sa.Text(), server_default="", nullable=False),
        sa.Column("open", sa.Numeric(12, 4), nullable=True),
        sa.Column("high", sa.Numeric(12, 4), nullable=True),
        sa.Column("low", sa.Numeric(12, 4), nullable=True),
        sa.Column("close", sa.Numeric(12, 4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("trade_date", "symbol"),
    )
    op.create_index("ix_daily_prices_symbol", "daily_prices", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_daily_prices_symbol", table_name="daily_prices")
    op.drop_table("daily_prices")

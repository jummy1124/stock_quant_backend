import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlmodel import Field, SQLModel

# Allowed screening-session identifiers (one snapshot per trade_date + session).
SESSION_INTRADAY_1300 = "intraday_1300"  # 盤中 13:00 篩選快照
SESSION_EOD = "eod"  # 收盤後篩選快照
SESSIONS = (SESSION_INTRADAY_1300, SESSION_EOD)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# NOTE: Models stay dialect-agnostic so tests can build the schema on SQLite via
# SQLModel.metadata.create_all(). UUID primary keys are generated client-side by
# default_factory. The Postgres-specific server defaults (gen_random_uuid()) live
# in the Alembic migration, which is the source of truth for the production schema.


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(Uuid(), primary_key=True),
    )
    email: str = Field(index=True, unique=True, nullable=False)
    password_hash: str = Field(nullable=False)
    display_name: str | None = Field(default=None, nullable=True)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
    )


class Record(SQLModel, table=True):
    __tablename__ = "records"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "market_code", "symbol", name="uq_records_user_market_symbol"
        ),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(Uuid(), primary_key=True),
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            Uuid(),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    market_code: str = Field(nullable=False)  # TWSE / TPEX
    symbol: str = Field(nullable=False)
    name: str = Field(default="", nullable=False)
    market: str = Field(default="", nullable=False)  # 上市 / 上櫃
    target_price: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    cost_price: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    last_close: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        ),
    )


# ---------------------------------------------------------------------------
# Screening snapshots (system-wide, not per-user).
#
# One ScreenSnapshot per (trade_date, session). `session` is one of SESSIONS:
#   - "intraday_1300": 盤中 13:00 那一刻篩選出來的個股
#   - "eod":           收盤後（最後交易日完成日K）篩選出來的個股
# Re-ingesting the same (trade_date, session) replaces its items (idempotent),
# so the screener can safely retry. Items hold the breakout (起漲) result rows.
# ---------------------------------------------------------------------------


class ScreenSnapshot(SQLModel, table=True):
    __tablename__ = "screen_snapshots"
    __table_args__ = (
        UniqueConstraint("trade_date", "session", name="uq_snapshot_date_session"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(Uuid(), primary_key=True),
    )
    trade_date: date = Field(sa_column=Column(Date(), nullable=False, index=True))
    session: str = Field(nullable=False)  # one of models.SESSIONS
    # Provenance / coverage metadata carried over from the screener snapshot.
    generated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    source: str = Field(default="", nullable=False)  # live / eod
    universe: int = Field(default=0, sa_column=Column(Integer(), nullable=False))
    quotable: int = Field(default=0, sa_column=Column(Integer(), nullable=False))
    pool_size: int = Field(default=0, sa_column=Column(Integer(), nullable=False))
    item_count: int = Field(default=0, sa_column=Column(Integer(), nullable=False))
    warning: str | None = Field(default=None, nullable=True)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
    )


class ScreenSnapshotItem(SQLModel, table=True):
    __tablename__ = "screen_snapshot_items"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(Uuid(), primary_key=True),
    )
    snapshot_id: uuid.UUID = Field(
        sa_column=Column(
            Uuid(),
            ForeignKey("screen_snapshots.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    rank: int = Field(default=0, sa_column=Column(Integer(), nullable=False))
    symbol: str = Field(nullable=False)
    name: str = Field(default="", nullable=False)
    market: str = Field(default="", nullable=False)  # 上市 / 上櫃
    market_code: str = Field(default="", nullable=False)  # TWSE / TPEX
    close: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    prev_close: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    change: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    change_pct: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    volume: int | None = Field(
        default=None, sa_column=Column(BigInteger(), nullable=True)
    )
    lots: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(16, 2), nullable=True)
    )
    open: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    high: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    low: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    # Breakout (起漲) detail columns.
    prev_high: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    vol_ratio: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    ma5: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    ma20: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    ma20_up: bool = Field(
        default=False, sa_column=Column(Boolean(), nullable=False)
    )

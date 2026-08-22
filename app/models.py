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
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlmodel import Field, SQLModel

# Allowed screening-session identifiers (one snapshot per trade_date + session).
SESSION_INTRADAY_1300 = "intraday_1300"  # 盤中 13:00 篩選快照
SESSION_EOD = "eod"  # 收盤後篩選快照
SESSIONS = (SESSION_INTRADAY_1300, SESSION_EOD)

# Purposes for the single-use tokens mailed to a user's address.
PURPOSE_VERIFY_EMAIL = "verify_email"
PURPOSE_PASSWORD_RESET = "password_reset"
TOKEN_PURPOSES = (PURPOSE_VERIFY_EMAIL, PURPOSE_PASSWORD_RESET)


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
    # NULL = address not verified yet. Verification is advisory (soft) in this
    # iteration: an unverified user can still sign in and use the app, the UI
    # just shows a banner. Keeping the timestamp (rather than a bool) makes the
    # audit question "when did they confirm?" answerable later.
    email_verified_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # Audit only: when the password last changed. Enforcement is token_version.
    password_changed_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
    )
    # Incremented on every password change and embedded in each JWT as `ver`.
    # get_current_user rejects any token whose `ver` doesn't match, which is how
    # "reset my password" ends other sessions without needing a token blacklist.
    #
    # A version counter rather than a timestamp comparison on purpose: JWT `iat`
    # has one-second granularity, so comparing it against a change timestamp
    # leaves a sub-second window in which a token minted just before the reset
    # still validates — precisely the token an attacker would be holding. An
    # integer either matches or it doesn't.
    token_version: int = Field(
        default=0,
        sa_column=Column(Integer(), nullable=False, server_default="0"),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
    )

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None


class EmailToken(SQLModel, table=True):
    """A single-use, time-limited token mailed to a user's address.

    Only the SHA-256 of the token is stored. The raw value exists exactly once,
    inside the email — so a database dump does not let an attacker verify
    addresses or reset passwords. SHA-256 (not bcrypt) is the right choice here
    because the token is 256 bits of `secrets` entropy: there is nothing to
    brute-force, and lookups must stay cheap.
    """

    __tablename__ = "email_tokens"

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
    purpose: str = Field(nullable=False)  # one of models.TOKEN_PURPOSES
    # Unique so a (theoretically impossible) collision surfaces as an error
    # rather than silently letting one token address two accounts.
    token_hash: str = Field(
        sa_column=Column(String(64), nullable=False, unique=True, index=True)
    )
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    used_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
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


# ---------------------------------------------------------------------------
# Whole-market daily closes (system-wide reference data).
#
# The screening snapshots above only ever contain the handful of stocks that
# passed the 起漲 filter on a given day. Backtesting needs the opposite: the
# price of *those* stocks on days when they were NOT selected — i.e. an ordinary
# daily bar for the whole market. That is what this table is.
#
# It is filled by the screener after close (it already holds the full-market
# history in memory, so uploading it costs no extra fetching) and by the
# backfill CLI. One row per (trade_date, symbol); re-uploading a day overwrites
# it, so retries are harmless.
#
# The distinct trade_date values in this table also double as the *trading
# calendar* the backtest counts "N 個交易日後" against — it is the market's own
# calendar, so holidays and typhoon days need no special-casing.
# ---------------------------------------------------------------------------


class DailyPrice(SQLModel, table=True):
    __tablename__ = "daily_prices"

    # Composite natural primary key rather than a surrogate UUID: this table
    # grows by ~1,800 rows per trading day (~450k/year), and (trade_date,
    # symbol) is exactly how the backtest reads it — "these symbols, on these
    # dates". A UUID column would add bytes and a second index for nothing.
    trade_date: date = Field(sa_column=Column(Date(), primary_key=True))
    symbol: str = Field(sa_column=Column(String(16), primary_key=True))

    name: str = Field(default="", nullable=False)
    market_code: str = Field(default="", nullable=False)  # TWSE / TPEX
    open: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    high: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    low: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(12, 4), nullable=True)
    )
    # The only column the backtest strictly needs; kept NOT NULL so a row can
    # never claim a trading day happened while offering no price for it.
    close: Decimal = Field(sa_column=Column(Numeric(12, 4), nullable=False))
    volume: int | None = Field(
        default=None, sa_column=Column(BigInteger(), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
    )

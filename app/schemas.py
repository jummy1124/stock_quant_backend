from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field


# ---------- Auth ----------


# bcrypt hashes at most 72 bytes and silently ignores the rest, so a longer
# password is not the extra security it looks like — cap it explicitly instead
# of letting the truncation happen invisibly.
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 72


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN)
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    # Deliberately NOT PASSWORD_MIN_LEN: accounts created before the policy
    # existed still have short passwords, and locking them out of sign-in (as
    # opposed to nudging them at the next change) would be a regression. The
    # max is kept so an oversized body is rejected before it reaches bcrypt.
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LEN)


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    email_verified: bool = False


class AuthResponse(BaseModel):
    token: str
    user: UserOut


# ---------- Email verification / password reset ----------


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1)
    password: str = Field(min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN)


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=1)


class MessageResponse(BaseModel):
    """Deliberately vague, uniform acknowledgement.

    Endpoints that act on an email address return this same shape whether or not
    the address exists, so the response can't be used to enumerate accounts.
    """

    message: str


# ---------- Records ----------


class UpsertBody(BaseModel):
    name: str = ""
    market: str = ""
    target_price: float | None = None
    cost_price: float | None = None
    last_close: float | None = None


class RecordOut(BaseModel):
    symbol: str
    name: str
    market: str
    market_code: str
    target_price: float | None = None
    cost_price: float | None = None
    last_close: float | None = None
    updated_at: datetime


class RecordsResponse(BaseModel):
    records: list[RecordOut]


# ---------- Screening snapshots (ingest) ----------


class SnapshotItemIn(BaseModel):
    """One screened (起漲) stock row coming from the screener."""

    rank: int = 0
    symbol: str
    name: str = ""
    market: str = ""
    market_code: str = ""
    close: float | None = None
    prev_close: float | None = None
    change: float | None = None
    change_pct: float | None = None
    volume: int | None = None
    lots: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_high: float | None = None
    vol_ratio: float | None = None
    ma5: float | None = None
    ma20: float | None = None
    ma20_up: bool = False


class SnapshotIngestBody(BaseModel):
    """Payload the screener POSTs once per (trade_date, session)."""

    trade_date: date
    session: str = Field(description="intraday_1300 | eod")
    generated_at: datetime
    source: str = ""  # live / eod
    universe: int = 0
    quotable: int = 0
    pool_size: int = 0
    warning: str | None = None
    items: list[SnapshotItemIn] = Field(default_factory=list)


class IngestResult(BaseModel):
    trade_date: date
    session: str
    item_count: int
    replaced: bool  # True if an existing snapshot for this date+session was overwritten


# ---------- Snapshot listing (download page) ----------


class SnapshotMeta(BaseModel):
    trade_date: date
    session: str
    generated_at: datetime
    source: str
    universe: int
    quotable: int
    pool_size: int
    item_count: int
    warning: str | None = None


class SnapshotListResponse(BaseModel):
    snapshots: list[SnapshotMeta]


class SnapshotCoverage(BaseModel):
    """Whole-table stats, used by the download page to show how much history
    actually exists in the database (independent of any single query's range).
    """

    min_date: date | None = None
    max_date: date | None = None
    trading_days: int = 0
    total_snapshots: int = 0
    db_size_bytes: int | None = Field(
        default=None,
        description=(
            "Total on-disk size (bytes) of the screening-snapshot tables "
            "(data + indexes + TOAST). None when the backing database doesn't "
            "support this (e.g. SQLite in tests)."
        ),
    )


# ---------- Whole-market daily prices (ingest) ----------


class DailyPriceIn(BaseModel):
    """One symbol's completed daily bar for the payload's trade_date.

    close is optional in the wire format but a bar without one is dropped on the
    way in: it carries no information the backtest can use, and rejecting the
    whole upload over a handful of untraded issues would be worse than skipping
    them.
    """

    symbol: str
    name: str = ""
    market_code: str = ""
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None


class DailyPricesIngestBody(BaseModel):
    """One trading day of whole-market closes, POSTed by the screener."""

    trade_date: date
    source: str = ""  # eod / backfill
    items: list[DailyPriceIn] = Field(default_factory=list)


class DailyPricesIngestResult(BaseModel):
    trade_date: date
    received: int
    inserted: int
    updated: int
    skipped: int  # rows dropped for having no close


# ---------- Backtest ----------

# The two comparisons the backtest supports. Both exit on a *closing* price;
# they differ in what counts as the entry.
#   intraday_to_close: 13:00 盤中價 (the intraday_1300 snapshot) -> 收盤價
#   close_to_close:    收盤價 (the eod snapshot)                 -> 收盤價
MODE_INTRADAY_TO_CLOSE = "intraday_to_close"
MODE_CLOSE_TO_CLOSE = "close_to_close"
BACKTEST_MODES = (MODE_INTRADAY_TO_CLOSE, MODE_CLOSE_TO_CLOSE)

# The snapshot session each mode draws its entries from.
MODE_SESSION = {
    MODE_INTRADAY_TO_CLOSE: "intraday_1300",
    MODE_CLOSE_TO_CLOSE: "eod",
}


class PriceCoverage(BaseModel):
    """How much whole-market price history exists — i.e. how far a backtest can
    actually reach. Shown next to the snapshot coverage so an empty result is
    self-explanatory ("no prices uploaded yet" vs "no stocks screened").
    """

    min_date: date | None = None
    max_date: date | None = None
    trading_days: int = 0
    total_rows: int = 0
    symbols: int = 0


class BacktestCoverage(BaseModel):
    """Everything the backtest page needs to set sensible date bounds."""

    snapshots: SnapshotCoverage
    prices: PriceCoverage


class BacktestHorizonStat(BaseModel):
    """Aggregate outcome of holding every screened stock for N trading days."""

    n: int
    samples: int = 0  # entries with both an entry and an exit price
    missing: int = 0  # entries dropped for a missing price on either end
    wins: int = 0  # return > 0
    losses: int = 0  # return < 0
    flat: int = 0  # return == 0
    win_rate: float | None = None  # wins / samples, 0..1; None when samples = 0
    avg_return_pct: float | None = None
    median_return_pct: float | None = None
    best_return_pct: float | None = None
    worst_return_pct: float | None = None


class BacktestDetailRow(BaseModel):
    """One screened stock's realised outcome at the horizon being detailed."""

    trade_date: date
    symbol: str
    name: str = ""
    market: str = ""
    market_code: str = ""
    entry_price: float
    exit_date: date
    exit_price: float
    change: float
    return_pct: float


class BacktestResponse(BaseModel):
    mode: str
    session: str
    start: date
    end: date
    horizons: list[int]
    # Entries considered before any price lookup — the denominator behind
    # "samples + missing" at every horizon.
    entries: int = 0
    trading_days: int = 0  # distinct screening days in range
    summary: list[BacktestHorizonStat] = Field(default_factory=list)
    detail_n: int = 0
    detail_total: int = 0  # rows available at detail_n, before the limit
    detail: list[BacktestDetailRow] = Field(default_factory=list)
    # Set when the answer is necessarily incomplete, e.g. the price table does
    # not yet reach far enough past the last screening day to settle horizon N.
    warning: str | None = None

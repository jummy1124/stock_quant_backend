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

from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field


# ---------- Auth ----------


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str | None = None


class AuthResponse(BaseModel):
    token: str
    user: UserOut


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

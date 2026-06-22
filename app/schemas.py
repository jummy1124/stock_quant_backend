from datetime import datetime

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

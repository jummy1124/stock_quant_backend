import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Numeric,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlmodel import Field, SQLModel


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

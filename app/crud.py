import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlmodel import Session, select

from app.models import Record, User
from app.schemas import UpsertBody


# ---------- Users ----------


def get_user_by_email(session: Session, email: str) -> User | None:
    return session.exec(select(User).where(User.email == email)).first()


def create_user(
    session: Session, email: str, password_hash: str, display_name: str | None
) -> User:
    user = User(
        email=email,
        password_hash=password_hash,
        display_name=display_name,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ---------- Records (always scoped by user_id) ----------


def _to_decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def list_records(session: Session, user_id: uuid.UUID) -> list[Record]:
    stmt = (
        select(Record)
        .where(Record.user_id == user_id)
        .order_by(Record.market_code, Record.symbol)
    )
    return list(session.exec(stmt).all())


def get_record(
    session: Session, user_id: uuid.UUID, market_code: str, symbol: str
) -> Record | None:
    stmt = select(Record).where(
        Record.user_id == user_id,
        Record.market_code == market_code,
        Record.symbol == symbol,
    )
    return session.exec(stmt).first()


def upsert_record(
    session: Session,
    user_id: uuid.UUID,
    market_code: str,
    symbol: str,
    body: UpsertBody,
) -> Record:
    record = get_record(session, user_id, market_code, symbol)
    if record is None:
        record = Record(
            user_id=user_id,
            market_code=market_code,
            symbol=symbol,
        )
        session.add(record)

    record.name = body.name
    record.market = body.market
    record.target_price = _to_decimal(body.target_price)
    record.cost_price = _to_decimal(body.cost_price)
    record.last_close = _to_decimal(body.last_close)
    record.updated_at = datetime.now(timezone.utc)

    session.commit()
    session.refresh(record)
    return record


def delete_record(
    session: Session, user_id: uuid.UUID, market_code: str, symbol: str
) -> bool:
    record = get_record(session, user_id, market_code, symbol)
    if record is None:
        return False
    session.delete(record)
    session.commit()
    return True

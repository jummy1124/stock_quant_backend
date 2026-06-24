import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import delete
from sqlmodel import Session, select

from app.models import Record, ScreenSnapshot, ScreenSnapshotItem, User
from app.schemas import SnapshotIngestBody, UpsertBody


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


# ---------- Screening snapshots ----------


def upsert_snapshot(
    session: Session, body: SnapshotIngestBody
) -> tuple[ScreenSnapshot, bool]:
    """Create or replace the snapshot for (trade_date, session).

    Idempotent: re-ingesting the same date+session deletes the previous items
    and rewrites the row, so the screener can safely retry. Returns
    (snapshot, replaced) where `replaced` is True when an existing snapshot was
    overwritten.
    """
    existing = session.exec(
        select(ScreenSnapshot).where(
            ScreenSnapshot.trade_date == body.trade_date,
            ScreenSnapshot.session == body.session,
        )
    ).first()
    replaced = existing is not None

    if existing is not None:
        session.execute(
            delete(ScreenSnapshotItem).where(
                ScreenSnapshotItem.snapshot_id == existing.id
            )
        )
        snapshot = existing
    else:
        snapshot = ScreenSnapshot(
            trade_date=body.trade_date, session=body.session
        )
        session.add(snapshot)

    snapshot.generated_at = body.generated_at
    snapshot.source = body.source
    snapshot.universe = body.universe
    snapshot.quotable = body.quotable
    snapshot.pool_size = body.pool_size
    snapshot.warning = body.warning
    snapshot.item_count = len(body.items)

    for item in body.items:
        session.add(
            ScreenSnapshotItem(
                snapshot_id=snapshot.id,
                rank=item.rank,
                symbol=item.symbol,
                name=item.name,
                market=item.market,
                market_code=item.market_code,
                close=_to_decimal(item.close),
                prev_close=_to_decimal(item.prev_close),
                change=_to_decimal(item.change),
                change_pct=_to_decimal(item.change_pct),
                volume=item.volume,
                lots=_to_decimal(item.lots),
                open=_to_decimal(item.open),
                high=_to_decimal(item.high),
                low=_to_decimal(item.low),
                prev_high=_to_decimal(item.prev_high),
                vol_ratio=_to_decimal(item.vol_ratio),
                ma5=_to_decimal(item.ma5),
                ma20=_to_decimal(item.ma20),
                ma20_up=item.ma20_up,
            )
        )

    session.commit()
    session.refresh(snapshot)
    return snapshot, replaced


def list_snapshots(
    session: Session, limit: int = 365
) -> list[ScreenSnapshot]:
    """Most-recent-first list of snapshot headers (no items)."""
    stmt = (
        select(ScreenSnapshot)
        .order_by(ScreenSnapshot.trade_date.desc(), ScreenSnapshot.session)
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def get_snapshot(
    session: Session, trade_date: date, session_name: str
) -> ScreenSnapshot | None:
    return session.exec(
        select(ScreenSnapshot).where(
            ScreenSnapshot.trade_date == trade_date,
            ScreenSnapshot.session == session_name,
        )
    ).first()


def get_snapshot_items(
    session: Session, snapshot_id: uuid.UUID
) -> list[ScreenSnapshotItem]:
    stmt = (
        select(ScreenSnapshotItem)
        .where(ScreenSnapshotItem.snapshot_id == snapshot_id)
        .order_by(ScreenSnapshotItem.rank)
    )
    return list(session.exec(stmt).all())

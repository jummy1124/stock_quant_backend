import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, func, text
from sqlmodel import Session, select

from app.models import (
    EmailToken,
    Record,
    ScreenSnapshot,
    ScreenSnapshotItem,
    User,
)
from app.schemas import SnapshotIngestBody, UpsertBody


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo on round-trip; re-attach UTC before comparing."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ---------- Users ----------


def normalize_email(email: str) -> str:
    """Case-insensitive, whitespace-trimmed form used for storage and lookup.

    Without this, `A@x.com` and `a@x.com` are two accounts, and "forgot
    password" silently fails for anyone who capitalised their address on a
    phone keyboard.
    """
    return email.strip().lower()


def get_user_by_email(session: Session, email: str) -> User | None:
    return session.exec(
        select(User).where(User.email == normalize_email(email))
    ).first()


def create_user(
    session: Session, email: str, password_hash: str, display_name: str | None
) -> User:
    user = User(
        email=normalize_email(email),
        password_hash=password_hash,
        display_name=display_name,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def mark_email_verified(session: Session, user: User) -> User:
    if user.email_verified_at is None:
        user.email_verified_at = _utcnow()
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def set_password(session: Session, user: User, password_hash: str) -> User:
    """Change the password and invalidate every token issued before now.

    Bumping ``token_version`` is what logs out any session still holding an
    older token — including the attacker's, which is the whole point of
    resetting a password you think was compromised. ``password_changed_at`` is
    recorded alongside it purely for the audit trail.
    """
    user.password_hash = password_hash
    user.password_changed_at = _utcnow()
    user.token_version = (user.token_version or 0) + 1
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ---------- Email tokens (verification / password reset) ----------


def create_email_token(
    session: Session, user_id: uuid.UUID, purpose: str, token_hash: str, ttl_minutes: int
) -> EmailToken:
    token = EmailToken(
        user_id=user_id,
        purpose=purpose,
        token_hash=token_hash,
        expires_at=_utcnow() + timedelta(minutes=ttl_minutes),
    )
    session.add(token)
    session.commit()
    session.refresh(token)
    return token


def get_usable_email_token(
    session: Session, purpose: str, token_hash: str
) -> EmailToken | None:
    """Look up a token that is for this purpose, unused, and not expired.

    ``purpose`` is part of the query on purpose: a verification token must never
    be accepted by the password-reset endpoint, even though both live in the
    same table.
    """
    token = session.exec(
        select(EmailToken).where(
            EmailToken.token_hash == token_hash,
            EmailToken.purpose == purpose,
        )
    ).first()
    if token is None or token.used_at is not None:
        return None
    expires_at = _as_utc(token.expires_at)
    if expires_at is None or expires_at <= _utcnow():
        return None
    return token


def consume_email_token(session: Session, token: EmailToken) -> EmailToken:
    token.used_at = _utcnow()
    session.add(token)
    session.commit()
    session.refresh(token)
    return token


def invalidate_email_tokens(
    session: Session, user_id: uuid.UUID, purpose: str
) -> int:
    """Mark every outstanding token of this purpose as used.

    Called before issuing a new one, so a mailbox never holds two live reset
    links, and called after a successful reset, so an older link in the same
    mailbox is dead on arrival.
    """
    rows = list(
        session.exec(
            select(EmailToken).where(
                EmailToken.user_id == user_id,
                EmailToken.purpose == purpose,
                EmailToken.used_at.is_(None),  # type: ignore[union-attr]
            )
        ).all()
    )
    now = _utcnow()
    for row in rows:
        row.used_at = now
        session.add(row)
    if rows:
        session.commit()
    return len(rows)


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


def list_snapshots_in_range(
    session: Session, start: date, end: date, session_name: str | None = None
) -> list[ScreenSnapshot]:
    """Snapshot headers within [start, end], oldest first (no items).

    session_name is optional: pass one of SESSIONS to filter to a single
    session, or omit it to get both sessions in the range.
    """
    stmt = select(ScreenSnapshot).where(
        ScreenSnapshot.trade_date >= start,
        ScreenSnapshot.trade_date <= end,
    )
    if session_name is not None:
        stmt = stmt.where(ScreenSnapshot.session == session_name)
    stmt = stmt.order_by(ScreenSnapshot.trade_date, ScreenSnapshot.session)
    return list(session.exec(stmt).all())


def get_snapshot_coverage(
    session: Session,
) -> tuple[date | None, date | None, int, int]:
    """(earliest trade_date, latest trade_date, distinct trading days, total
    snapshot rows) across the whole table — a cheap SQL aggregate, accurate
    regardless of how many rows exist (no in-memory cap needed).
    """
    total = session.exec(
        select(func.count()).select_from(ScreenSnapshot)
    ).one()
    if not total:
        return None, None, 0, 0
    min_date = session.exec(select(func.min(ScreenSnapshot.trade_date))).one()
    max_date = session.exec(select(func.max(ScreenSnapshot.trade_date))).one()
    trading_days = session.exec(
        select(func.count(func.distinct(ScreenSnapshot.trade_date)))
    ).one()
    return min_date, max_date, trading_days, total


def get_snapshot_db_size(session: Session) -> int | None:
    """Total on-disk size (bytes) of the screening-snapshot tables — data,
    indexes, and TOAST — via Postgres's pg_total_relation_size().

    Returns None on non-Postgres engines (e.g. the SQLite test database),
    where this figure isn't meaningful/available; callers should treat that
    as "unknown", not zero.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return None
    result = session.execute(
        text(
            "SELECT pg_total_relation_size('screen_snapshots') "
            "+ pg_total_relation_size('screen_snapshot_items')"
        )
    ).scalar()
    return int(result) if result is not None else None


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

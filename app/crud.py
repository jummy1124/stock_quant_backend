import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, func, text
from sqlmodel import Session, select

from app.models import (
    BranchTrade,
    DailyPrice,
    EmailToken,
    Record,
    ScreenSnapshot,
    ScreenSnapshotItem,
    User,
)
from app.schemas import BranchTradesIngestBody, DailyPricesIngestBody, SnapshotIngestBody, UpsertBody


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


def list_snapshot_entries(
    session: Session, start: date, end: date, session_name: str
) -> list[tuple[date, ScreenSnapshotItem]]:
    """Every screened stock in [start, end] for one session, as
    (trade_date, item) pairs, oldest first.

    A single join rather than "list the snapshots, then fetch each one's items":
    a multi-year backtest spans hundreds of snapshots, and the per-snapshot
    version would issue hundreds of round-trips to assemble the same rows.
    """
    stmt = (
        select(ScreenSnapshot.trade_date, ScreenSnapshotItem)
        .join(ScreenSnapshotItem, ScreenSnapshotItem.snapshot_id == ScreenSnapshot.id)
        .where(
            ScreenSnapshot.trade_date >= start,
            ScreenSnapshot.trade_date <= end,
            ScreenSnapshot.session == session_name,
        )
        .order_by(ScreenSnapshot.trade_date, ScreenSnapshotItem.rank)
    )
    return [(row[0], row[1]) for row in session.exec(stmt).all()]


# ---------- Branch trades ----------

def upsert_branch_trades(session: Session, body: BranchTradesIngestBody) -> int:
    existing = {r.symbol: r for r in session.exec(select(BranchTrade).where(
        BranchTrade.trade_date == body.trade_date, BranchTrade.branch_code == body.branch_code)).all()}
    for item in body.items:
        row = existing.get(item.symbol)
        if row is None:
            row = BranchTrade(trade_date=body.trade_date, branch_code=body.branch_code, symbol=item.symbol)
            session.add(row)
        row.branch_name = body.branch_name or item.branch_name
        row.stock_name = item.stock_name
        row.buy_amount = item.buy_amount; row.sell_amount = item.sell_amount; row.net_amount = item.net_amount
        row.inventory_cost = item.inventory_cost; row.inventory_value = item.inventory_value
        row.fetched_at = _utcnow()
    session.commit()
    return len(body.items)


def list_branch_trades(session: Session, branch_code: str, start: date, end: date) -> list[BranchTrade]:
    return list(session.exec(select(BranchTrade).where(
        BranchTrade.branch_code == branch_code,
        BranchTrade.trade_date >= start, BranchTrade.trade_date <= end,
    ).order_by(BranchTrade.trade_date, BranchTrade.net_amount.desc())).all())


# ---------- Daily prices (whole-market closes; the backtest's price source) ----------


def upsert_daily_prices(session: Session, body: DailyPricesIngestBody) -> tuple[int, int]:
    """Insert or overwrite one trading day's bars. Returns (inserted, updated).

    Read-then-write rather than a dialect-specific ON CONFLICT: the payload is
    one day of one market (~1,800 rows at most), so the extra SELECT is cheap,
    and the same code path works on Postgres in production and SQLite in tests.

    Deliberately *not* "delete the day, then insert": the backfill CLI and the
    live screener can each upload a partial day (one market, or only the symbols
    it could quote), and a delete-first strategy would let the second upload
    silently destroy the first one's rows.
    """
    existing = {
        row.symbol: row
        for row in session.exec(
            select(DailyPrice).where(DailyPrice.trade_date == body.trade_date)
        ).all()
    }
    inserted = updated = 0
    for item in body.items:
        if item.close is None:
            # A bar with no close says nothing the backtest can use, and the
            # column is NOT NULL. Skip rather than fail the whole upload.
            continue
        row = existing.get(item.symbol)
        if row is None:
            row = DailyPrice(trade_date=body.trade_date, symbol=item.symbol)
            session.add(row)
            inserted += 1
        else:
            updated += 1
        row.name = item.name or (row.name if row.name else "")
        row.market_code = item.market_code or (row.market_code if row.market_code else "")
        row.open = _to_decimal(item.open)
        row.high = _to_decimal(item.high)
        row.low = _to_decimal(item.low)
        row.close = _to_decimal(item.close)
        row.volume = item.volume
    session.commit()
    return inserted, updated


def list_price_trading_days(session: Session) -> list[date]:
    """Every trading day the price table knows about, ascending.

    This *is* the trading calendar the backtest counts "N 個交易日後" against.
    Deriving it from the data rather than from a holiday list means market
    closures — weekends, national holidays, typhoon days, unscheduled halts —
    are handled by construction: a day the whole market did not trade simply has
    no rows, so it is never counted.
    """
    stmt = select(DailyPrice.trade_date).distinct().order_by(DailyPrice.trade_date)
    return list(session.exec(stmt).all())


def get_price_coverage(session: Session) -> tuple[date | None, date | None, int, int, int]:
    """(earliest, latest, trading days, total rows, distinct symbols) for the
    price table — a cheap SQL aggregate, so it stays accurate at any size.
    """
    total = session.exec(select(func.count()).select_from(DailyPrice)).one()
    if not total:
        return None, None, 0, 0, 0
    min_date = session.exec(select(func.min(DailyPrice.trade_date))).one()
    max_date = session.exec(select(func.max(DailyPrice.trade_date))).one()
    days = session.exec(
        select(func.count(func.distinct(DailyPrice.trade_date)))
    ).one()
    symbols = session.exec(select(func.count(func.distinct(DailyPrice.symbol)))).one()
    return min_date, max_date, days, total, symbols


# Chunk size for the symbol IN (...) lists below. Postgres tolerates far more,
# but SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 999 — staying under it
# keeps the test suite running against the same code path as production.
_SYMBOL_CHUNK = 500


def load_close_prices(
    session: Session, wanted: Mapping[date, Iterable[str]]
) -> dict[tuple[str, date], float]:
    """{(symbol, trade_date): close} for exactly the pairs asked for.

    Takes a {trade_date: symbols} map rather than a date range on purpose. The
    backtest needs a sparse scatter of pairs — each screened stock on its own
    handful of exit days — and a range query would have to fetch the whole
    symbols × days rectangle around them to be sure of covering it. On a year of
    real data that is an order of magnitude more rows read and converted than
    the caller will ever look at (measured: ~434k fetched to answer ~35k), and
    the difference is seconds of wall clock on the "全部歷史" query.

    One statement per date (chunked by symbol count) keeps every statement on
    the (trade_date, symbol) primary key.
    """
    out: dict[tuple[str, date], float] = {}
    for trade_date, symbols in wanted.items():
        unique = sorted({s for s in symbols if s})
        for i in range(0, len(unique), _SYMBOL_CHUNK):
            chunk = unique[i : i + _SYMBOL_CHUNK]
            stmt = select(DailyPrice.symbol, DailyPrice.close).where(
                DailyPrice.trade_date == trade_date,
                DailyPrice.symbol.in_(chunk),  # type: ignore[attr-defined]
            )
            for symbol, close in session.exec(stmt).all():
                if close is not None:
                    out[(symbol, trade_date)] = float(close)
    return out

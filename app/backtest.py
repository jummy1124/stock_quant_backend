"""Forward-return backtest over the stored screening snapshots.

The question it answers: of every stock this system has ever flagged as 起漲,
what fraction was worth more N trading days later — and by how much?

Two entry conventions, matching the two snapshots the screener uploads daily:

    intraday_to_close   進場 = 盤中 13:00 快照當下的價格 (intraday_1300)
                        出場 = 第 N 個交易日的收盤價   (N = 0 → 當日收盤)

    close_to_close      進場 = 收盤快照的收盤價        (eod)
                        出場 = 第 N 個交易日的收盤價   (N ≥ 1)

Both exit on a close; only the entry differs. That is why 13:00-vs-close can be
asked at N = 0 (buy at one o'clock, mark to market at the bell) while
close-to-close cannot — at N = 0 it would compare a price with itself.

Two things this module is deliberate about:

*Counting trading days, not calendar days.* "第 N 個交易日" is resolved against
the distinct dates present in `daily_prices`, i.e. the market's own record of
when it was open. Weekends, national holidays and typhoon days therefore need no
special-casing: a day nobody traded has no rows and is never counted. An entry
day missing from that calendar (prices not uploaded for it) still works — the
Nth day *after* it is found by binary search rather than by index arithmetic.

*Separating "no trade" from "no data".* An entry whose exit price is unknown is
counted as `missing`, never as a flat or losing trade. Silently treating an
absent price as a zero return would drag every win rate toward the middle in
exactly the periods where price coverage is thinnest.

⚠️ 統計為歷史資訊參考，非投資建議。
"""
from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlmodel import Session

from app import crud
from app.schemas import (
    DEFAULT_DETAIL_SORT,
    DETAIL_SORT_KEYS,
    MODE_CLOSE_TO_CLOSE,
    MODE_SESSION,
    BacktestDetailRow,
    BacktestHorizonStat,
    BacktestResponse,
)

# Guard rails for the public endpoint. The horizon cap keeps one request from
# walking the whole calendar per entry; the horizon-count cap bounds the work at
# entries × horizons.
MAX_HORIZON = 120
MAX_HORIZONS = 16
DEFAULT_HORIZONS = (1, 2, 3, 5, 10, 20)


@dataclass(slots=True)
class _Entry:
    """One screened stock on one screening day, with its entry price resolved."""

    trade_date: date
    symbol: str
    name: str
    market: str
    market_code: str
    entry_price: float | None


def _f(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def nth_trading_day(
    calendar: list[date], day: date, n: int
) -> date | None:
    """The Nth trading day strictly after `day` (N ≥ 1); `day` itself for N = 0.

    `calendar` must be ascending and duplicate-free. Returns None when the
    calendar does not reach that far — which is the normal state of affairs for
    the most recent screening days, whose N-day outcome has not happened yet.

    Binary search rather than "look up day's index and add N" on purpose: the
    entry day is not guaranteed to be in the price calendar (prices for it may
    never have been uploaded), and bisect gives the right answer either way —
    the first trading day *after* it is well defined regardless.
    """
    if n < 0:
        return None
    if n == 0:
        pos = bisect_left(calendar, day)
        if pos < len(calendar) and calendar[pos] == day:
            return day
        return None
    idx = bisect_right(calendar, day) + n - 1
    return calendar[idx] if idx < len(calendar) else None


def normalize_horizons(values: list[int]) -> list[int]:
    """Sorted, de-duplicated, in-range horizons. Raises ValueError if unusable."""
    cleaned = sorted({int(v) for v in values})
    if not cleaned:
        raise ValueError("至少需要一個 N 值。")
    if any(v < 0 for v in cleaned):
        raise ValueError("N 不可為負數。")
    if any(v > MAX_HORIZON for v in cleaned):
        raise ValueError(f"N 最大為 {MAX_HORIZON}。")
    if len(cleaned) > MAX_HORIZONS:
        raise ValueError(f"一次最多查詢 {MAX_HORIZONS} 個 N 值。")
    return cleaned


def _summarize(n: int, returns: list[float], missing: int) -> BacktestHorizonStat:
    """Aggregate one horizon's realised returns.

    `flat` is its own bucket rather than being folded into wins or losses: at
    N = 0 a stock that has not moved since 13:00 is common, and counting those
    as wins would quietly inflate the headline probability.
    """
    if not returns:
        return BacktestHorizonStat(n=n, samples=0, missing=missing)
    wins = sum(1 for r in returns if r > 0)
    losses = sum(1 for r in returns if r < 0)
    flat = len(returns) - wins - losses
    return BacktestHorizonStat(
        n=n,
        samples=len(returns),
        missing=missing,
        wins=wins,
        losses=losses,
        flat=flat,
        win_rate=round(wins / len(returns), 6),
        avg_return_pct=round(statistics.fmean(returns), 4),
        median_return_pct=round(statistics.median(returns), 4),
        best_return_pct=round(max(returns), 4),
        worst_return_pct=round(min(returns), 4),
    )


def _collect_entries(
    session: Session, start: date, end: date, session_name: str
) -> list[_Entry]:
    return [
        _Entry(
            trade_date=trade_date,
            symbol=item.symbol,
            name=item.name or "",
            market=item.market or "",
            market_code=item.market_code or "",
            entry_price=_f(item.close),
        )
        for trade_date, item in crud.list_snapshot_entries(
            session, start, end, session_name
        )
    ]


def run_backtest(
    session: Session,
    *,
    mode: str,
    start: date,
    end: date,
    horizons: list[int],
    detail_n: int | None = None,
    detail_limit: int = 500,
    detail_sort: str = DEFAULT_DETAIL_SORT,
    detail_order: str = "desc",
) -> BacktestResponse:
    """Run the whole-database backtest and return the response payload."""
    if detail_sort not in DETAIL_SORT_KEYS:
        raise ValueError(f"排序欄位需為 {DETAIL_SORT_KEYS} 其中之一。")
    if detail_order not in ("asc", "desc"):
        raise ValueError("排序方向需為 asc 或 desc。")
    session_name = MODE_SESSION[mode]
    horizons = normalize_horizons(horizons)
    if detail_n is None or detail_n not in horizons:
        detail_n = horizons[0]

    entries = _collect_entries(session, start, end, session_name)
    calendar = crud.list_price_trading_days(session)
    screening_days = len({e.trade_date for e in entries})

    empty = BacktestResponse(
        mode=mode,
        session=session_name,
        start=start,
        end=end,
        horizons=horizons,
        entries=len(entries),
        trading_days=screening_days,
        summary=[BacktestHorizonStat(n=n, missing=len(entries)) for n in horizons],
        detail_n=detail_n,
        detail_sort=detail_sort,
        detail_order=detail_order,
    )
    if not entries:
        empty.warning = "此區間與時段查無篩選紀錄。"
        return empty
    if not calendar:
        empty.warning = (
            "資料庫尚無全市場每日收盤價，無法計算後續漲跌。"
            "請先執行收盤價上傳/回補 (run_backfill_prices.py)。"
        )
        return empty

    # Resolve every (entry day, horizon) to an exit day once, up front: the same
    # handful of screening days is shared by thousands of entries, so doing this
    # per entry would repeat the same binary searches over and over.
    exit_days: dict[tuple[date, int], date] = {}
    for day in {e.trade_date for e in entries}:
        for n in horizons:
            target = nth_trading_day(calendar, day, n)
            if target is not None:
                exit_days[(day, n)] = target

    # Ask the database for exactly the (symbol, day) pairs the loop below will
    # look up — a scatter, not a rectangle. See crud.load_close_prices for why
    # a date-range query is the wrong shape here.
    wanted: dict[date, set[str]] = {}
    for entry in entries:
        for n in horizons:
            exit_day = exit_days.get((entry.trade_date, n))
            if exit_day is not None:
                wanted.setdefault(exit_day, set()).add(entry.symbol)
        if mode == MODE_CLOSE_TO_CLOSE and entry.entry_price is None:
            # Only this mode can recover a missing entry price from the price
            # table; the 13:00 price exists nowhere but the snapshot.
            wanted.setdefault(entry.trade_date, set()).add(entry.symbol)
    prices = crud.load_close_prices(session, wanted)

    returns: dict[int, list[float]] = {n: [] for n in horizons}
    missing: dict[int, int] = {n: 0 for n in horizons}
    detail: list[BacktestDetailRow] = []

    for entry in entries:
        # close_to_close's entry is a closing price, so the price table can
        # supply it when the snapshot row didn't record one. The 13:00 price has
        # no such fallback — it exists only in the snapshot.
        entry_price = entry.entry_price
        if entry_price is None and mode == MODE_CLOSE_TO_CLOSE:
            entry_price = prices.get((entry.symbol, entry.trade_date))
        if entry_price is None or entry_price <= 0:
            for n in horizons:
                missing[n] += 1
            continue

        for n in horizons:
            exit_day = exit_days.get((entry.trade_date, n))
            exit_price = (
                None if exit_day is None else prices.get((entry.symbol, exit_day))
            )
            if exit_day is None or exit_price is None:
                missing[n] += 1
                continue
            change = exit_price - entry_price
            pct = change / entry_price * 100.0
            returns[n].append(pct)
            if n == detail_n:
                detail.append(
                    BacktestDetailRow(
                        trade_date=entry.trade_date,
                        symbol=entry.symbol,
                        name=entry.name,
                        market=entry.market,
                        market_code=entry.market_code,
                        entry_price=round(entry_price, 4),
                        exit_date=exit_day,
                        exit_price=round(exit_price, 4),
                        change=round(change, 4),
                        return_pct=round(pct, 4),
                    )
                )

    # Sort the WHOLE result set, then cut. Doing it the other way round — cut to
    # the newest 500 and let the browser sort those — would answer "which trade
    # returned the most?" with the best of the most recent 500, and look no
    # different from the real answer. detail_total tells the client how much was
    # left behind.
    detail.sort(key=_detail_sort_key(detail_sort), reverse=(detail_order == "desc"))
    detail_total = len(detail)
    if detail_limit >= 0:
        detail = detail[:detail_limit]

    warning = _coverage_warning(entries, calendar, horizons, missing)

    return BacktestResponse(
        mode=mode,
        session=session_name,
        start=start,
        end=end,
        horizons=horizons,
        entries=len(entries),
        trading_days=screening_days,
        summary=[_summarize(n, returns[n], missing[n]) for n in horizons],
        detail_n=detail_n,
        detail_total=detail_total,
        detail_sort=detail_sort,
        detail_order=detail_order,
        detail=detail,
        warning=warning,
    )


def _detail_sort_key(key: str):
    """Sort key for one detail column, with a stable tie-break.

    Every column falls back to (trade_date, symbol) so equal values — two stocks
    that both returned exactly 0%, say — keep a fixed, reproducible order rather
    than shuffling between requests and making the table look unstable.
    """
    if key == "trade_date":
        return lambda r: (r.trade_date, r.symbol)
    if key == "symbol":
        return lambda r: (r.symbol, r.trade_date)
    if key == "exit_date":
        return lambda r: (r.exit_date, r.trade_date, r.symbol)
    return lambda r: (getattr(r, key), r.trade_date, r.symbol)


def _coverage_warning(
    entries: list[_Entry],
    calendar: list[date],
    horizons: list[int],
    missing: dict[int, int],
) -> str | None:
    """A plain-language note when the result is necessarily incomplete.

    Two distinct causes, and the difference matters to the reader: the newest
    screening days simply have not had N trading days happen yet (nothing to
    fix, wait), versus prices never having been uploaded for days that are long
    past (fixable, run the backfill).
    """
    last_entry_day = max(e.trade_date for e in entries)
    last_price_day = calendar[-1]
    biggest = max(horizons)
    if nth_trading_day(calendar, last_entry_day, biggest) is None:
        return (
            f"價格資料只到 {last_price_day:%Y-%m-%d}，最近幾個篩選日尚未經過 "
            f"{biggest} 個交易日，這些個股不列入統計（見「無資料」欄）。"
        )
    worst = max(missing.values(), default=0)
    if worst and worst >= len(entries) * 0.2:
        return (
            f"有 {worst} 筆缺少對應的收盤價而未列入統計，"
            "可能是該期間的全市場收盤價尚未回補。"
        )
    return None

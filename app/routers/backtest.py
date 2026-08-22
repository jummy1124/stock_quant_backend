"""Backtest endpoints — public, under /backtestapi.

Access model matches the download module: screening snapshots and whole-market
closes are system-wide reference data, not anybody's private records, so these
read-only endpoints need no JWT.

The heavy lifting lives in app/backtest.py; this module is the HTTP edge —
parsing and validating query parameters, and turning the result into JSON or a
workbook.
"""
from datetime import date as date_cls

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlmodel import Session

from app import crud
from app.backtest import (
    DEFAULT_HORIZONS,
    MAX_HORIZON,
    MAX_HORIZONS,
    run_backtest,
)
from app.backtest_xlsx import backtest_to_xlsx, empty_backtest_xlsx
from app.db import get_session
from app.schemas import (
    BACKTEST_MODES,
    MODE_CLOSE_TO_CLOSE,
    BacktestCoverage,
    BacktestResponse,
    PriceCoverage,
    SnapshotCoverage,
)

router = APIRouter(prefix="/backtestapi", tags=["backtest"])

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# Five years of calendar days. Generous enough that "全部歷史" is a normal
# request, bounded enough that a stray query can't ask for a century.
_MAX_RANGE_DAYS = 366 * 5
_MAX_DETAIL_ROWS = 5000


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
    )


def _parse_horizons(raw: str | None) -> list[int]:
    """"1,2,3,5,10" -> [1, 2, 3, 5, 10]; empty/None -> the defaults."""
    if raw is None or not raw.strip():
        return list(DEFAULT_HORIZONS)
    values: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(int(part))
        except ValueError:
            raise _bad_request(f"N 需為整數，收到 {part!r}。") from None
    if not values:
        raise _bad_request("至少需要一個 N 值。")
    return values


def _resolve_range(
    session: Session, start: date_cls | None, end: date_cls | None
) -> tuple[date_cls, date_cls]:
    """Fill in whichever end of the range the caller left open.

    Omitting both means "every record in the database", which is the page's
    default view — so the bounds come from the snapshot table itself rather than
    from a guessed date, and stay right as history accumulates.
    """
    if start is None or end is None:
        min_date, max_date, _, _ = crud.get_snapshot_coverage(session)
        start = start or min_date or date_cls.today()
        end = end or max_date or date_cls.today()
    if start > end:
        raise _bad_request("起始日期不可晚於結束日期。")
    if (end - start).days > _MAX_RANGE_DAYS:
        raise _bad_request(f"查詢區間過長（最多 {_MAX_RANGE_DAYS} 天）。")
    return start, end


def _validated_mode(mode: str) -> str:
    if mode not in BACKTEST_MODES:
        raise _bad_request(f"mode 需為 {BACKTEST_MODES} 其中之一。")
    return mode


def _run(
    session: Session,
    mode: str,
    start: date_cls | None,
    end: date_cls | None,
    horizons_raw: str | None,
    detail_n: int | None,
    detail_limit: int,
) -> BacktestResponse:
    mode = _validated_mode(mode)
    start, end = _resolve_range(session, start, end)
    horizons = _parse_horizons(horizons_raw)
    # N = 0 means "same-day close", which only makes sense against a 13:00 entry;
    # for close-to-close it would compare a price with itself and report a 0%
    # return for every stock, which reads as data rather than as nonsense.
    if mode == MODE_CLOSE_TO_CLOSE and any(n < 1 for n in horizons):
        raise _bad_request("收盤價 → 收盤價 模式的 N 需 ≥ 1（N=0 等於自己比自己）。")
    try:
        return run_backtest(
            session,
            mode=mode,
            start=start,
            end=end,
            horizons=horizons,
            detail_n=detail_n,
            detail_limit=detail_limit,
        )
    except ValueError as exc:
        raise _bad_request(str(exc)) from None


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/coverage", response_model=BacktestCoverage)
def backtest_coverage(session: Session = Depends(get_session)):
    """How far the backtest can reach, from both sides.

    Snapshot coverage bounds which screening days can be tested; price coverage
    bounds how far past them an outcome can be settled. Reporting both is what
    lets the page explain an empty or partial result instead of just showing
    zeroes.
    """
    s_min, s_max, s_days, s_total = crud.get_snapshot_coverage(session)
    p_min, p_max, p_days, p_rows, p_symbols = crud.get_price_coverage(session)
    return BacktestCoverage(
        snapshots=SnapshotCoverage(
            min_date=s_min,
            max_date=s_max,
            trading_days=s_days,
            total_snapshots=s_total,
            db_size_bytes=crud.get_snapshot_db_size(session),
        ),
        prices=PriceCoverage(
            min_date=p_min,
            max_date=p_max,
            trading_days=p_days,
            total_rows=p_rows,
            symbols=p_symbols,
        ),
    )


@router.get("/run", response_model=BacktestResponse)
def backtest_run(
    mode: str = Query(
        "close_to_close",
        description="intraday_to_close（13:00→收盤）| close_to_close（收盤→收盤）",
    ),
    start: date_cls | None = Query(None, description="起始篩選日 YYYY-MM-DD（省略=最早）"),
    end: date_cls | None = Query(None, description="結束篩選日 YYYY-MM-DD（省略=最新）"),
    horizons: str | None = Query(
        None,
        description=f"要統計的 N 值，逗號分隔，例 1,2,3,5,10（最多 {MAX_HORIZONS} 個，"
        f"單一 N 上限 {MAX_HORIZON}）",
    ),
    detail_n: int | None = Query(
        None, description="明細表要列出哪一個 N（需在 horizons 內；省略=第一個）"
    ),
    detail_limit: int = Query(
        500, ge=0, le=_MAX_DETAIL_ROWS, description="明細表最多回幾筆"
    ),
    session: Session = Depends(get_session),
):
    """Public: win rate and return distribution for every screened stock in range."""
    return _run(session, mode, start, end, horizons, detail_n, detail_limit)


@router.get("/backtest.xlsx")
def backtest_xlsx(
    mode: str = Query("close_to_close"),
    start: date_cls | None = Query(None),
    end: date_cls | None = Query(None),
    horizons: str | None = Query(None),
    detail_n: int | None = Query(None),
    session: Session = Depends(get_session),
):
    """Public: the same run as /run, as a workbook.

    The detail sheet is capped higher than the JSON endpoint's default — a file
    being saved for offline analysis wants the rows, where a web table does not.
    """
    result = _run(session, mode, start, end, horizons, detail_n, _MAX_DETAIL_ROWS)
    filename = (
        f"backtest_{result.start:%Y%m%d}_{result.end:%Y%m%d}"
        f"_{result.mode}_N{result.detail_n}.xlsx"
    )
    if result.entries == 0:
        data = empty_backtest_xlsx(
            f"{result.start:%Y-%m-%d} ~ {result.end:%Y-%m-%d} "
            f"（{result.session}）區間內查無篩選紀錄。"
        )
    else:
        data = backtest_to_xlsx(result)
    return Response(
        content=data,
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

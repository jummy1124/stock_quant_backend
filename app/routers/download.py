"""Download module — self-contained so it can be split into its own project.

Exposes everything under the /downloadapi prefix and depends only on:
  - crud / models / db   (read snapshots + records)
  - download_xlsx        (build the .xlsx bytes)
  - security             (JWT, only for the per-user records download)

Access model (per product decision):
  - Screening snapshots are system-wide reference data -> PUBLIC (no auth).
  - A user's own records are private -> require the existing user JWT.

To extract later: move this file + app/download_xlsx.py (and the snapshot
crud/model bits) into a new service; the contract below stays identical.
"""
from datetime import date as date_cls

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlmodel import Session

from app import crud
from app.db import get_session
from app.download_xlsx import (
    empty_snapshot_xlsx,
    empty_snapshots_range_xlsx,
    records_to_xlsx,
    snapshot_to_xlsx,
    snapshots_range_to_xlsx,
)
from app.models import SESSIONS, User
from app.schemas import SnapshotCoverage, SnapshotListResponse, SnapshotMeta
from app.security import get_current_user

router = APIRouter(prefix="/downloadapi", tags=["download"])

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MAX_RANGE_DAYS = 366  # guard against pathological queries; ~1 year of calendar days


def _xlsx_response(data: bytes, filename: str) -> Response:
    # filename is ASCII-only here, so a plain Content-Disposition is enough.
    return Response(
        content=data,
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/coverage", response_model=SnapshotCoverage)
def snapshot_coverage(session: Session = Depends(get_session)):
    """Public: whole-database stats (earliest/latest trade_date, trading days,
    total snapshot rows) via a cheap SQL aggregate — accurate no matter how
    much history has accumulated (unlike scanning a capped listing client-side).
    """
    min_date, max_date, trading_days, total = crud.get_snapshot_coverage(session)
    return SnapshotCoverage(
        min_date=min_date,
        max_date=max_date,
        trading_days=trading_days,
        total_snapshots=total,
    )


@router.get("/snapshots", response_model=SnapshotListResponse)
def list_snapshots(
    limit: int = Query(365, ge=1, le=2000),
    start: date_cls | None = Query(
        None, description="起始交易日 YYYY-MM-DD（需與 end 一起帶）"
    ),
    end: date_cls | None = Query(
        None, description="結束交易日 YYYY-MM-DD（需與 start 一起帶）"
    ),
    session_name: str | None = Query(
        None, alias="session", description="intraday_1300 | eod（可選，篩選單一時段）"
    ),
    session: Session = Depends(get_session),
):
    """Public: available screening snapshots, headers only.

    Two modes:
      - no start/end -> most-recent-`limit` snapshots (newest first), for a
        quick overview.
      - start & end given -> every snapshot in that closed range (oldest
        first), queried straight from the database — this is what the
        download page's date-range picker uses, so results always reflect
        what's actually persisted, not a client-side cache.
    """
    if start is not None or end is not None:
        if start is None or end is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="start and end must be provided together.",
            )
        if start > end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="start must not be after end.",
            )
        if session_name is not None and session_name not in SESSIONS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"session must be one of {SESSIONS}.",
            )
        rows = crud.list_snapshots_in_range(session, start, end, session_name)
    else:
        rows = crud.list_snapshots(session, limit=limit)

    return SnapshotListResponse(
        snapshots=[
            SnapshotMeta(
                trade_date=s.trade_date,
                session=s.session,
                generated_at=s.generated_at,
                source=s.source,
                universe=s.universe,
                quotable=s.quotable,
                pool_size=s.pool_size,
                item_count=s.item_count,
                warning=s.warning,
            )
            for s in rows
        ]
    )


@router.get("/snapshot.xlsx")
def download_snapshot(
    date: date_cls = Query(..., description="交易日 YYYY-MM-DD"),
    session_name: str = Query(
        ..., alias="session", description="intraday_1300 | eod"
    ),
    session: Session = Depends(get_session),
):
    """Public: download one screening snapshot as .xlsx."""
    if session_name not in SESSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"session must be one of {SESSIONS}.",
        )
    snap = crud.get_snapshot(session, date, session_name)
    filename = f"screen_{date:%Y%m%d}_{session_name}.xlsx"
    if snap is None:
        # No snapshot for that day -> return a clearly-labelled empty workbook
        # (200, not 404) so the browser still downloads something explanatory.
        data = empty_snapshot_xlsx(f"{date:%Y-%m-%d}", session_name)
        return _xlsx_response(data, filename)
    items = crud.get_snapshot_items(session, snap.id)
    data = snapshot_to_xlsx(snap, items)
    return _xlsx_response(data, filename)


@router.get("/snapshots.xlsx")
def download_snapshots_range(
    start: date_cls = Query(..., description="起始交易日 YYYY-MM-DD"),
    end: date_cls = Query(..., description="結束交易日 YYYY-MM-DD"),
    session_name: str = Query(
        ..., alias="session", description="intraday_1300 | eod"
    ),
    session: Session = Depends(get_session),
):
    """Public: download every screening snapshot in [start, end] for one session,
    packed into a single .xlsx (one row per stock, with a leading 交易日 column).
    """
    if session_name not in SESSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"session must be one of {SESSIONS}.",
        )
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start must not be after end.",
        )
    if (end - start).days > _MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"range too large (max {_MAX_RANGE_DAYS} days).",
        )

    start_label, end_label = f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}"
    filename = f"screen_{start:%Y%m%d}_{end:%Y%m%d}_{session_name}.xlsx"

    snaps = crud.list_snapshots_in_range(session, start, end, session_name)
    if not snaps:
        data = empty_snapshots_range_xlsx(start_label, end_label, session_name)
        return _xlsx_response(data, filename)

    rows = [(s, crud.get_snapshot_items(session, s.id)) for s in snaps]
    data = snapshots_range_to_xlsx(rows, start_label, end_label, session_name)
    return _xlsx_response(data, filename)


@router.get("/records.xlsx")
def download_records(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Private: the logged-in user's own records as .xlsx (requires JWT)."""
    records = crud.list_records(session, current_user.id)
    owner = current_user.display_name or current_user.email
    data = records_to_xlsx(records, owner_label=owner)
    return _xlsx_response(data, "my_records.xlsx")

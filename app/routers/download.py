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
    records_to_xlsx,
    snapshot_to_xlsx,
)
from app.models import SESSIONS, User
from app.schemas import SnapshotListResponse, SnapshotMeta
from app.security import get_current_user

router = APIRouter(prefix="/downloadapi", tags=["download"])

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


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


@router.get("/snapshots", response_model=SnapshotListResponse)
def list_snapshots(
    limit: int = Query(365, ge=1, le=2000),
    session: Session = Depends(get_session),
):
    """Public: available screening snapshots (newest first), headers only."""
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

"""Ingestion endpoint for daily screening snapshots.

The screener (stock_market `run_intraday.py`) POSTs here twice a day:
  - at 13:00 with session="intraday_1300"
  - after close with session="eod"

Service-to-service auth via the X-Ingest-Token header (shared secret in
settings.INGEST_TOKEN). This is intentionally separate from the user JWT auth:
the screener is a backend job, not a logged-in user. Constant-time compare to
avoid leaking the token via timing.
"""
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlmodel import Session

from app import crud
from app.config import settings
from app.db import get_session
from app.models import SESSIONS
from app.schemas import IngestResult, SnapshotIngestBody

router = APIRouter(prefix="/userapi/ingest", tags=["ingest"])


def require_ingest_token(
    x_ingest_token: str | None = Header(default=None, alias="X-Ingest-Token"),
) -> None:
    configured = settings.INGEST_TOKEN
    if not configured:
        # Fail closed: refuse ingestion until a token is configured.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion is not configured (INGEST_TOKEN unset).",
        )
    if not x_ingest_token or not hmac.compare_digest(x_ingest_token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Ingest-Token.",
        )


@router.post(
    "/snapshot",
    response_model=IngestResult,
    dependencies=[Depends(require_ingest_token)],
)
def ingest_snapshot(
    body: SnapshotIngestBody,
    session: Session = Depends(get_session),
):
    if body.session not in SESSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"session must be one of {SESSIONS}, got {body.session!r}.",
        )
    snapshot, replaced = crud.upsert_snapshot(session, body)
    return IngestResult(
        trade_date=snapshot.trade_date,
        session=snapshot.session,
        item_count=snapshot.item_count,
        replaced=replaced,
    )

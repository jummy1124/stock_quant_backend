"""Ingestion endpoint for daily screening snapshots.

The screener (stock_market `run_intraday.py`) POSTs here twice a day:
  - at 13:00 with session="intraday_1300"
  - after close with session="eod"

After close it also POSTs one day of whole-market closing prices per request to
/prices. Those are what the backtest measures the screened stocks against — the
snapshots alone cannot say what a stock was worth on a later day it was not
selected. The screener already holds the full-market history in memory, so
uploading it costs no additional fetching from the exchange.

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
from app.schemas import (
    DailyPricesIngestBody,
    DailyPricesIngestResult,
    IngestResult,
    SnapshotIngestBody,
)

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


@router.post(
    "/prices",
    response_model=DailyPricesIngestResult,
    dependencies=[Depends(require_ingest_token)],
)
def ingest_prices(
    body: DailyPricesIngestBody,
    session: Session = Depends(get_session),
):
    """Insert or overwrite one trading day of whole-market closing prices.

    Idempotent per (trade_date, symbol), and additive rather than
    replace-the-day: the screener uploads what it can quote and the backfill CLI
    fills in the rest, possibly in either order, and neither should be able to
    erase the other's rows.
    """
    received = len(body.items)
    inserted, updated = crud.upsert_daily_prices(session, body)
    return DailyPricesIngestResult(
        trade_date=body.trade_date,
        received=received,
        inserted=inserted,
        updated=updated,
        skipped=received - inserted - updated,
    )

from fastapi import APIRouter, Depends, Response, status
from sqlmodel import Session

from app import crud
from app.db import get_session
from app.models import Record, User
from app.schemas import RecordOut, RecordsResponse, UpsertBody
from app.security import get_current_user

router = APIRouter(prefix="/userapi/records", tags=["records"])


def _to_float(value) -> float | None:
    return None if value is None else float(value)


def _record_out(record: Record) -> RecordOut:
    return RecordOut(
        symbol=record.symbol,
        name=record.name,
        market=record.market,
        market_code=record.market_code,
        target_price=_to_float(record.target_price),
        cost_price=_to_float(record.cost_price),
        last_close=_to_float(record.last_close),
        updated_at=record.updated_at,
    )


@router.get("", response_model=RecordsResponse)
def list_records(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    records = crud.list_records(session, current_user.id)
    return RecordsResponse(records=[_record_out(r) for r in records])


@router.put("/{market_code}/{symbol}", response_model=RecordOut)
def upsert_record(
    market_code: str,
    symbol: str,
    body: UpsertBody,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    record = crud.upsert_record(
        session, current_user.id, market_code, symbol, body
    )
    return _record_out(record)


@router.delete("/{market_code}/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
def delete_record(
    market_code: str,
    symbol: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    # Idempotent delete: missing record (incl. another user's) returns 204.
    crud.delete_record(session, current_user.id, market_code, symbol)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

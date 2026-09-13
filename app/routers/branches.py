from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session
from app import crud
from app.db import get_session
from app.schemas import BranchTradesIngestBody, BranchTradesResponse, BranchTradeOut
from app.routers.ingest import require_ingest_token

router = APIRouter(prefix="/userapi/branches", tags=["branch trades"])

@router.get("/{branch_code}", response_model=BranchTradesResponse)
def get_branch_trades(branch_code: str, start: date, end: date, session: Session = Depends(get_session)):
    if start > end: raise HTTPException(400, "start must not be after end")
    rows = crud.list_branch_trades(session, branch_code, start, end)
    return BranchTradesResponse(branch_code=branch_code, branch_name=rows[0].branch_name if rows else "", start=start, end=end,
        trades=[BranchTradeOut.model_validate(r, from_attributes=True) for r in rows])

@router.post("/ingest", dependencies=[Depends(require_ingest_token)])
def ingest_branch_trades(body: BranchTradesIngestBody, session: Session = Depends(get_session)):
    return {"trade_date": body.trade_date, "branch_code": body.branch_code, "count": crud.upsert_branch_trades(session, body)}

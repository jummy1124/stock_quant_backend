from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import auth, download, ingest, records

app = FastAPI(title="stock_quant_userdata", version="0.1.0")

_origins = settings.allowed_origins_list
# allow_credentials=True is incompatible with the "*" wildcard per the CORS spec.
# Auth uses a Bearer header (not cookies), so credentials aren't required for "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=_origins != ["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router)
app.include_router(records.router)
app.include_router(ingest.router)
app.include_router(download.router)


@app.get("/health")
def health():
    return {"status": "ok"}

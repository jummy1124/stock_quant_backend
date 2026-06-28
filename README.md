# Stock Quant — User-Data Backend

> A small, focused **multi-user backend** for personal stock records (watchlist, target
> price, cost price) plus snapshot storage and spreadsheet downloads. It is fully
> decoupled from the screening engine and the web UI: it **never fetches prices or runs
> screening** — it only stores user accounts and per-user data, with strict isolation.

**Tech:** FastAPI · SQLModel / SQLAlchemy 2.0 · PostgreSQL 16 · Alembic · JWT auth · Poetry · Docker

This is the **persistence/API service** of a three-part system. Companion repositories:
the screening engine [`stock_market`](https://github.com/jummy1124/stock_quant) and the
React UI `stock_quant_frontend`.

---

## Why it exists

The screening engine is stateless and public — it computes the same market view for
everyone. The moment users want their *own* watchlist, target prices and history, you need
accounts, authorization and a database. I deliberately split that concern into its own
service so the crawler stays simple and stateless, and all the security-sensitive parts
(password hashing, JWT, per-user isolation, migrations) live in one auditable place.

## Highlights

- **Clean, layered FastAPI app**: routers → CRUD → models, with every DB query scoped by
  `user_id` so users can never read or write each other's data.
- **Real auth done correctly**: bcrypt password hashing + stateless JWT (HS256), secrets
  read from the environment only — nothing hard-coded, `.env` git-ignored.
- **Schema migrations with Alembic**, applied automatically on container start.
- **Same-origin by design**: everything is served under the `/userapi` (and `/downloadapi`)
  prefix so the frontend's nginx can reverse-proxy to it with no CORS in production.
- **Idempotent, predictable API**: `PUT` upserts, `DELETE` is idempotent, cross-user access
  is treated as not-found.
- **Tested offline**: the pytest suite runs against in-memory SQLite — no Postgres needed.

## System architecture

```mermaid
flowchart LR
    subgraph ENG[stock_market<br/>screening engine :8000]
      SNAP[daily snapshots]
    end

    subgraph BE["stock_quant_backend — user-data API (THIS REPO)"]
      API[FastAPI :8100]
      AUTH["/userapi/auth · /userapi/me"]
      REC["/userapi/records"]
      DL["/downloadapi · ingest"]
      PG[(PostgreSQL 16)]
      API --- AUTH
      API --- REC
      API --- DL
      AUTH --- PG
      REC --- PG
      DL --- PG
    end

    subgraph FE[stock_quant_frontend<br/>React SPA + nginx]
      UI[Browser UI]
    end

    SNAP -->|"POST snapshot + X-Ingest-Token"| DL
    UI -->|register / login / records| API
    UI -->|download .xlsx| DL

    style BE fill:#eef6ff,stroke:#2563eb
```

## Tech stack

| Area | Choice |
|---|---|
| Framework | FastAPI |
| ORM / models | SQLModel on SQLAlchemy 2.0 |
| Database | PostgreSQL 16 (`psycopg`), Alembic migrations |
| Auth | JWT HS256 (`pyjwt`) + bcrypt password hashing (`passlib`) |
| Packaging | Poetry (`pyproject.toml` + `poetry.lock`) |
| Spreadsheets | `openpyxl` (records / snapshot `.xlsx` export) |
| Tests | `pytest` + `httpx` against in-memory SQLite |

## Getting started

### Prerequisites
- Docker + docker compose (simplest path), **or**
- Python **3.11+**, [Poetry](https://python-poetry.org/), and a running PostgreSQL

### Option A — Run with Docker (recommended)

```bash
git clone <this-repo> && cd stock_quant_backend
cp .env.example .env            # then set a real JWT_SECRET (and INGEST_TOKEN if used)
docker compose up --build
```

Startup order is handled for you: **Postgres becomes healthy → the app runs
`alembic upgrade head` → Uvicorn starts.** Then verify:

```bash
curl http://localhost:8100/health        # {"status":"ok"}
# Swagger UI: http://localhost:8100/docs
```

### Option B — Run locally with Poetry

```bash
poetry install                            # venv + all deps (incl. dev)
cp .env.example .env                      # point DATABASE_URL at your Postgres
poetry run alembic upgrade head           # create the schema on a clean DB
poetry run uvicorn app.main:app --reload --port 8100
```

### Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://user:pass@localhost:5432/userdata` | sync psycopg driver |
| `JWT_SECRET` | — | **set a long random string** |
| `JWT_EXPIRE_MINUTES` | `1440` | token lifetime |
| `ALLOWED_ORIGINS` | `*` | comma-separated; tighten in production |
| `INGEST_TOKEN` | — | shared secret the screener sends to POST daily snapshots (empty = disabled) |
| `APP_PORT` | `8100` | |
| `POSTGRES_USER/PASSWORD/DB` | `user/pass/userdata` | used by docker compose |

Secrets are read from the environment only and never hard-coded; `.env` is git-ignored.

## API

All paths are prefixed with `/userapi`. Everything except register/login requires
`Authorization: Bearer <jwt>`. Full interactive docs at `/docs`.

### Auth

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/userapi/auth/register` | `{email, password, display_name?}` | `201 {token, user}` |
| POST | `/userapi/auth/login` | `{email, password}` | `200 {token, user}` |
| POST | `/userapi/auth/logout` | — | `204` (stateless; client drops the token) |
| GET | `/userapi/me` | — | `200 user` |

`user` = `{ id, email, display_name }`. Errors: bad credentials → `401`; duplicate email →
`409`; validation → `422`.

### Records

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/userapi/records` | — | `200 {records: Record[]}` |
| PUT | `/userapi/records/{market_code}/{symbol}` | UpsertBody | `200 Record` |
| DELETE | `/userapi/records/{market_code}/{symbol}` | — | `204` |

```jsonc
// UpsertBody
{ "name": "TSMC", "market": "TWSE",
  "target_price": 120.0, "cost_price": 95.5, "last_close": 109.5 }
```

- **PUT is an upsert** keyed on `(user_id, market_code, symbol)`.
- **DELETE is idempotent**; deleting a missing or another user's record returns `204`.
- Users can only access their own records; anything else is treated as not-found.

### Downloads & snapshot ingest

The service also exposes a `/downloadapi` area for exporting a user's records and the daily
screening snapshots as `.xlsx`, plus an authenticated ingest endpoint the screening engine
posts to (guarded by the `X-Ingest-Token` header). See [`DOWNLOAD.md`](DOWNLOAD.md) for the
full contract.

## Project structure

```
app/
  main.py        FastAPI app, CORS, router mounting, /health
  config.py      Settings (env vars)
  db.py          engine / session dependency
  security.py    password hashing, JWT, get_current_user
  models.py      SQLModel: User, Record (+ snapshots)
  schemas.py     request/response models (snake_case)
  crud.py        DB access (always scoped by user_id)
  download_xlsx.py  openpyxl exporters
  routers/
    auth.py      /userapi/auth/*, /userapi/me
    records.py   /userapi/records*
    download.py  /downloadapi/*
    ingest.py    snapshot ingest (X-Ingest-Token)
alembic/         migrations
tests/           pytest suite (in-memory SQLite)
```

## Testing

```bash
poetry install
poetry run pytest
```

Runs against in-memory SQLite (no Postgres required) and covers the auth flow, records
CRUD, **user isolation**, and auth-error handling.

## Non-goals (by design)

- No price fetching, indicators, or screening — that's `stock_market`.
- No frontend pages — that's `stock_quant_frontend`.
- No refresh tokens, third-party OAuth, or email verification in this iteration.

---

*Disclaimer: this project is for technical and educational purposes and stores
user-entered data only. It provides no financial advice.*

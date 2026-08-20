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
  read from the environment only — nothing hard-coded, `.env` git-ignored, and the app
  refuses to boot without a strong `JWT_SECRET` rather than falling back to a default.
- **Email verification & password reset** with single-use, hashed, expiring tokens; the
  reset flow bumps a per-user token version, so changing your password really does sign
  out every other session. Sending is pluggable (`console` for dev, `smtp` for real)
  and adds no third-party dependency.
- **Rate limiting** on sign-in, sign-up and the mail-sending endpoints, and deliberate
  anti-enumeration: `forgot-password` returns the same 202 whether or not the address
  is registered.
- **Schema migrations with Alembic**, applied automatically on container start.
- **Same-origin by design**: everything is served under the `/userapi` (and `/downloadapi`)
  prefix so the frontend's nginx can reverse-proxy to it with no CORS in production.
- **Idempotent, predictable API**: `PUT` upserts, `DELETE` is idempotent, cross-user access
  is treated as not-found.
- **Tested offline**: the pytest suite runs against in-memory SQLite — no Postgres needed.

## System architecture

```mermaid
flowchart LR
    subgraph ENG["stock_market · screening engine :8000"]
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

    subgraph FE["stock_quant_frontend · React SPA + nginx"]
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
| Email | `smtplib` + `email.message` (standard library — no extra dependency) |
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
| `JWT_SECRET` | **required** | ≥32 chars; the app refuses to start without it |
| `JWT_EXPIRE_MINUTES` | `1440` | token lifetime |
| `ALLOWED_ORIGINS` | `http://localhost:5173` | comma-separated, or `*` |
| `INGEST_TOKEN` | — | shared secret the screener sends to POST daily snapshots (empty = disabled) |
| `APP_PORT` | `8100` | |
| `APP_BASE_URL` | `http://localhost:5173` | frontend URL used to build links inside emails |
| `EMAIL_BACKEND` | `console` | `console` (log the message) or `smtp` (send it) |
| `EMAIL_FROM` / `EMAIL_FROM_NAME` | `no-reply@localhost` / `Stock Quant` | envelope sender |
| `SMTP_HOST/PORT/USER/PASSWORD` | — | only when `EMAIL_BACKEND=smtp` |
| `SMTP_STARTTLS` / `SMTP_SSL` | `true` / `false` | port 587 submission vs. port 465 implicit TLS |
| `EMAIL_VERIFY_TTL_MINUTES` | `1440` | verification link lifetime |
| `PASSWORD_RESET_TTL_MINUTES` | `30` | reset link lifetime |
| `RATE_LIMIT_ENABLED` | `true` | in-process limiter; see `app/ratelimit.py` before scaling out |
| `POSTGRES_USER/PASSWORD/DB` | `user/pass/userdata` | used by docker compose |

Secrets are read from the environment only and never hard-coded; `.env` is git-ignored.

> **`JWT_SECRET` has no default on purpose.** A fallback value means a service that
> boots misconfigured signs tokens with a key that is in the public repository —
> anyone can then forge a token for any user. Failing to start is the safe outcome.
> Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

### Email in development

`EMAIL_BACKEND=console` (the default) writes the whole message — including the
verification / reset link — to the application log, so the full flow is testable with
no SMTP account:

```
docker compose logs -f app     # or watch the uvicorn output
```

For real delivery with Gmail, set `EMAIL_BACKEND=smtp`, `SMTP_HOST=smtp.gmail.com`,
`SMTP_PORT=587`, and use an [App Password](https://myaccount.google.com/apppasswords)
as `SMTP_PASSWORD`.

## API

All paths are prefixed with `/userapi`. Everything requires
`Authorization: Bearer <jwt>` except the public entry points: register, login,
`verify-email`, `forgot-password` and `reset-password` — each of those is reached
either before you have a token or from a link in an email, where possession of the
single-use token *is* the credential. Full interactive docs at `/docs`.

### Auth

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/userapi/auth/register` | `{email, password, display_name?}` | `201 {token, user}` |
| POST | `/userapi/auth/login` | `{email, password}` | `200 {token, user}` |
| POST | `/userapi/auth/logout` | — | `204` (stateless; client drops the token) |
| GET | `/userapi/me` | — | `200 user` |
| POST | `/userapi/auth/verify-email` | `{token}` | `200 user` (no auth needed) |
| POST | `/userapi/auth/resend-verification` | — | `202 {message}` (auth required) |
| POST | `/userapi/auth/forgot-password` | `{email}` | `202 {message}` |
| POST | `/userapi/auth/reset-password` | `{token, password}` | `200 {token, user}` |

`user` = `{ id, email, display_name, email_verified }`. Errors: bad credentials → `401`;
duplicate email → `409`; validation → `422`; invalid/expired/used link → `400`;
rate-limited → `429` with `Retry-After`.

**Passwords** must be 8–72 characters at registration and reset (72 is bcrypt's limit,
past which it silently truncates). Sign-in still accepts shorter passwords so accounts
created before the policy keep working.

**Email verification is advisory.** Registration returns a token immediately and an
unverified user can do everything; the UI just shows a banner. A bounced verification
email is a nuisance, never a lockout.

**`forgot-password` never reveals whether an address is registered** — same status,
same body, either way. Reset links are single-use and expire; a successful reset also
marks the address verified (clicking a link we mailed there is proof of control) and
invalidates every token issued earlier, so the session that prompted the reset is
signed out.

> ⚠️ **Deploy note:** tokens minted before this version carry no `ver` claim and are
> rejected, so everyone is signed out once on upgrade. That is deliberate — accepting
> version-less tokens would leave a window where a password reset revokes nothing.

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
  config.py      Settings (env vars), with fail-fast validation
  db.py          engine / session dependency
  security.py    password hashing, JWT, email-token hashing, get_current_user
  email.py       pluggable sender (console / SMTP) + message templates
  ratelimit.py   in-process sliding-window limiter for the auth endpoints
  models.py      SQLModel: User, Record, EmailToken (+ snapshots)
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
CRUD, **user isolation**, auth-error handling, both email flows (verification and
password reset), token expiry / reuse / cross-purpose misuse, anti-enumeration, the
rate limiter, and the config guard rails.

Mail never leaves the process: the `outbox` fixture swaps in a capturing sender, so the
tests read the real link out of the real message body.

## Non-goals (by design)

- No price fetching, indicators, or screening — that's `stock_market`.
- No frontend pages — that's `stock_quant_frontend`.
- No refresh tokens or third-party OAuth (social sign-in) in this iteration.
- No MFA.
- The rate limiter is in-process, so it does not coordinate across workers or replicas.

---

*Disclaimer: this project is for technical and educational purposes and stores
user-entered data only. It provides no financial advice.*

# stock_quant_userdata

Standalone multi-user backend for personal **stock records** (target price / cost price),
fully decoupled from the screening crawler (`stock_market`) and the frontend
(`stock_quant_frontend`). It does **not** fetch prices or run screening — it only stores
user accounts and per-user stock records, with strict user isolation.

- Framework: FastAPI + SQLModel (SQLAlchemy 2.0)
- DB: PostgreSQL 16 (`psycopg`), migrations via Alembic
- Auth: JWT (HS256, `pyjwt`) + bcrypt password hashing (`passlib`)
- Dependency management: Poetry (`pyproject.toml` + `poetry.lock`)
- Path prefix: `/userapi` (served same-origin behind the frontend nginx in production)

## Project layout

```
app/
  main.py        FastAPI app, CORS, router mounting, /health
  config.py      Settings (env vars)
  db.py          engine / session dependency
  security.py    password hashing, JWT, get_current_user
  models.py      SQLModel: User, Record
  schemas.py     request/response models (snake_case)
  crud.py        DB access (always scoped by user_id)
  routers/
    auth.py      /userapi/auth/*, /userapi/me
    records.py   /userapi/records*
alembic/         migrations
tests/           pytest suite (SQLite in-memory)
```

## Configuration

Copy `.env.example` to `.env` and adjust:

```
DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/userdata
JWT_SECRET=<random long string>
JWT_EXPIRE_MINUTES=1440
ALLOWED_ORIGINS=*          # comma-separated; tighten in production
APP_PORT=8100
```

Secrets are read from the environment only — never hard-coded. `.env` is git-ignored.

## Run with Docker (recommended)

```bash
cp .env.example .env        # then edit JWT_SECRET
docker compose up --build
```

Startup order: Postgres becomes healthy → the `userdata` container runs
`alembic upgrade head` → uvicorn starts. Then:

```bash
curl http://localhost:8100/health     # {"status":"ok"}
```

## Run locally (without Docker)

This project uses [Poetry](https://python-poetry.org/). Install it first
(`pipx install poetry` or see the Poetry docs), then:

```bash
poetry install                          # creates a venv and installs all deps (incl. dev)
cp .env.example .env                     # point DATABASE_URL at a running Postgres
poetry run alembic upgrade head          # create schema on a clean DB
poetry run uvicorn app.main:app --reload --port 8100
```

`poetry install` generates/uses `poetry.lock` for reproducible installs. Commit the
lock file once generated. Use `poetry add <pkg>` / `poetry add --group dev <pkg>` to
manage dependencies instead of editing `pyproject.toml` by hand.

## API

All paths are prefixed with `/userapi`. Everything except register/login requires
`Authorization: Bearer <jwt>`.

### Auth

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/userapi/auth/register` | `{email, password, display_name?}` | `201 {token, user}` |
| POST | `/userapi/auth/login` | `{email, password}` | `200 {token, user}` |
| POST | `/userapi/auth/logout` | — | `204` (stateless; client drops the token) |
| GET  | `/userapi/me` | — | `200 user` |

`user` shape: `{ "id", "email", "display_name" }`.
JWT payload contains at least `sub` (= user id) and `exp`.

Errors: wrong credentials → `401 {detail}`; duplicate email → `409 {detail}`;
validation error → `422`.

### Records

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/userapi/records` | — | `200 {records: Record[]}` |
| PUT | `/userapi/records/{market_code}/{symbol}` | UpsertBody | `200 Record` |
| DELETE | `/userapi/records/{market_code}/{symbol}` | — | `204` |

UpsertBody:

```json
{ "name": "台積電", "market": "上市",
  "target_price": 120.0, "cost_price": 95.5, "last_close": 109.5 }
```

Record (response, snake_case):

```json
{
  "symbol": "2330", "name": "台積電", "market": "上市", "market_code": "TWSE",
  "target_price": 120.0, "cost_price": 95.5, "last_close": 109.5,
  "updated_at": "2026-06-21T14:08:00Z"
}
```

- **PUT is an upsert** keyed on `(user_id, market_code, symbol)`: inserts if absent,
  otherwise overwrites and refreshes `updated_at`.
- **DELETE is idempotent**: deleting a record that does not exist (including another
  user's record) returns `204`.
- Users can only read/write their own records; accessing someone else's data is
  treated as not-found.

## Tests

```bash
poetry install
poetry run pytest
```

The suite runs against an in-memory SQLite database (no Postgres needed) and covers the
auth flow, records CRUD, user isolation, and auth-error handling.

## Notes / non-goals

- No price fetching, technical indicators, or screening (that is `stock_market`).
- No frontend pages.
- No refresh tokens, third-party OAuth, or email verification in this iteration.

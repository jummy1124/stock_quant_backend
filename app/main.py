import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import auth, download, ingest, records


def _configure_logging() -> None:
    """Give this application's own loggers a handler.

    Uvicorn installs handlers for *its* loggers ("uvicorn", "uvicorn.error",
    "uvicorn.access") and deliberately leaves the root logger alone. Anything we
    log under `app.*` therefore propagates to a root logger with no handler, and
    Python falls back to `logging.lastResort` — which only emits WARNING and
    above. Net effect: every `logger.info()` in this codebase silently vanishes
    in production while working fine under pytest (which captures at INFO).

    That is not hypothetical. The console email backend writes the verification
    and password-reset links at INFO so they can be copied out of the log when no
    SMTP server is configured, and on the deployed VM they were being discarded —
    the feature looked broken with nothing in the log to explain it.
    """
    level = getattr(logging, settings.LOG_LEVEL.strip().upper(), logging.INFO)
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )
        app_logger.addHandler(handler)
    app_logger.setLevel(level)
    # Don't also hand these to the root logger; that would double-print them
    # anywhere root *is* configured.
    app_logger.propagate = False


def _warn_about_dev_defaults() -> None:
    """Say so at boot when a development default is live in a real deployment.

    Both of these fail *silently* and look like a broken feature rather than a
    missing setting: with the console backend the user is told "check your
    inbox" and nothing is ever sent, and with the placeholder base URL the mail
    goes out carrying a link to localhost. Neither is worth a hard failure — the
    console backend is genuinely the right default for development — but both
    are worth one loud line in the log.
    """
    log = logging.getLogger("app.startup")

    if settings.EMAIL_BACKEND == "console":
        log.warning(
            "EMAIL_BACKEND=console — verification and password-reset emails will "
            "be written to this log and NOT delivered to anyone. Set "
            "EMAIL_BACKEND=smtp plus SMTP_HOST/PORT/USER/PASSWORD to send for real."
        )
    elif not settings.SMTP_HOST:
        log.warning(
            "EMAIL_BACKEND=smtp but SMTP_HOST is empty — falling back to the "
            "console backend; no mail will be delivered."
        )

    if "localhost" in settings.APP_BASE_URL or "127.0.0.1" in settings.APP_BASE_URL:
        log.warning(
            "APP_BASE_URL=%s — links inside outgoing emails will point there. "
            "Set it to the address a recipient's browser can actually reach.",
            settings.APP_BASE_URL,
        )


_configure_logging()
_warn_about_dev_defaults()

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

"""Guard rails for log visibility.

These exist because of a real production failure: the console email backend
wrote the password-reset link at INFO, uvicorn leaves the root logger without a
handler, and Python's lastResort handler only emits WARNING and above. The link
was discarded on the server, so "forgot password" looked broken with nothing in
the log to explain it.

The tests below pin the two properties that would have caught it.
"""
import logging

from app.email import ConsoleEmailSender, send_email
from tests.conftest import register, token_from_email


def test_app_logger_has_a_handler():
    """app.main._configure_logging must attach one, or app.* output is dropped
    wherever the root logger is unconfigured (i.e. under uvicorn)."""
    import app.main  # noqa: F401  — importing runs _configure_logging()

    assert logging.getLogger("app").handlers, (
        "The 'app' logger has no handler: every logger.info()/warning() in this "
        "codebase would fall through to logging.lastResort and be filtered."
    )


def test_console_backend_logs_at_warning_or_above(caplog):
    """Below WARNING it is invisible under a default production log config."""
    sender = ConsoleEmailSender()
    with caplog.at_level(logging.DEBUG, logger="app.email"):
        sender.send("someone@example.com", "Subject here", "body with a link")

    records = [r for r in caplog.records if r.name == "app.email"]
    assert records, "console backend logged nothing at all"
    assert max(r.levelno for r in records) >= logging.WARNING, (
        "console backend logged below WARNING; it would be invisible in "
        "production, which is exactly the bug this test exists to prevent."
    )


def test_console_backend_log_contains_the_link(caplog):
    """The whole point of this backend is that the link is recoverable."""
    with caplog.at_level(logging.DEBUG, logger="app.email"):
        send_email("someone@example.com", "Verify", "open http://x/?verify=TOKEN123")

    assert "TOKEN123" in caplog.text


def test_send_failure_is_logged_at_error(caplog):
    """A transport failure must be loud — callers deliberately swallow it so the
    HTTP response can't be used to enumerate accounts, which means the log is the
    only place it can surface."""
    from app import email as email_mod

    class _Broken:
        def send(self, *a, **k):
            raise ConnectionRefusedError("smtp down")

    email_mod.set_email_sender(_Broken())
    try:
        with caplog.at_level(logging.DEBUG, logger="app.email"):
            ok = send_email("a@example.com", "Subject", "body")
    finally:
        email_mod.set_email_sender(None)

    assert ok is False
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a failed send produced no ERROR log line"
    assert "smtp down" in caplog.text


def test_forgot_password_failure_does_not_change_the_response(client, caplog):
    """Delivery failure must stay invisible to the client (anti-enumeration)
    while still being visible in the log."""
    from app import email as email_mod

    register(client, email="quiet@example.com", password="secret123")

    class _Broken:
        def send(self, *a, **k):
            raise ConnectionRefusedError("smtp down")

    email_mod.set_email_sender(_Broken())
    try:
        resp = client.post(
            "/userapi/auth/forgot-password", json={"email": "quiet@example.com"}
        )
    finally:
        email_mod.set_email_sender(None)

    assert resp.status_code == 202
    assert "has an account" in resp.json()["message"]


def test_reset_link_is_recoverable_from_the_console_backend(client, outbox):
    """End-to-end shape of the dev workflow: no SMTP, link out of the message."""
    register(client, email="devflow@example.com", password="secret123")
    outbox.clear()
    client.post("/userapi/auth/forgot-password", json={"email": "devflow@example.com"})

    token = token_from_email(outbox[0][2], "reset")
    resp = client.post(
        "/userapi/auth/reset-password",
        json={"token": token, "password": "new-password-1"},
    )
    assert resp.status_code == 200

"""Outgoing transactional email (verification + password reset).

Two interchangeable senders, selected by the ``EMAIL_BACKEND`` setting:

* ``console`` (default) — renders the message to the application log. No SMTP
  account required, and the verification / reset link is right there in the log,
  which is what you want during development and in the test suite.
* ``smtp`` — sends for real over SMTP using nothing but the standard library
  (``smtplib`` + ``email.message``), so this adds zero third-party dependencies.

Both implement the same tiny :class:`EmailSender` protocol, so swapping in a
third-party API sender later (Resend, SendGrid, SES) means adding one class and
one ``EMAIL_BACKEND`` value — no caller changes.

Sending never raises into a request handler: :func:`send_email` catches and logs
transport failures and returns ``False``. Callers deliberately do not surface
that to the client, because "we couldn't reach your mailbox" is exactly the kind
of signal an account-enumeration probe is looking for.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from functools import lru_cache
from typing import Protocol

from app.config import settings

logger = logging.getLogger("app.email")


class EmailSender(Protocol):
    """Anything that can deliver one plain-text (+ optional HTML) message."""

    def send(
        self, to: str, subject: str, text_body: str, html_body: str | None = None
    ) -> None:
        ...


class ConsoleEmailSender:
    """Writes the message to the log instead of sending it.

    Deliberately prints the full body: during development the whole point is to
    be able to copy the verification / reset link out of the terminal.

    Logged at WARNING, not INFO. "We generated a password-reset link and did not
    actually deliver it to anyone" is a noteworthy state of the system, not
    routine chatter — and if this backend is ever active in production by
    accident (it is the default, so that is an easy mistake), the log line needs
    to survive whatever level the deployment is filtering at.
    """

    def send(
        self, to: str, subject: str, text_body: str, html_body: str | None = None
    ) -> None:
        logger.warning(
            "\n"
            "=========== EMAIL *NOT SENT* — console backend, no SMTP ===============\n"
            "To:      %s\n"
            "From:    %s <%s>\n"
            "Subject: %s\n"
            "-----------------------------------------------------------------------\n"
            "%s\n"
            "=======================================================================",
            to,
            settings.EMAIL_FROM_NAME,
            settings.EMAIL_FROM,
            subject,
            text_body,
        )


class SmtpEmailSender:
    """Sends over SMTP with the standard library.

    Supports both common submission styles:
      * port 587 + STARTTLS (``SMTP_STARTTLS=true``) — Gmail, most providers
      * port 465 + implicit TLS (``SMTP_SSL=true``)
    """

    def send(
        self, to: str, subject: str, text_body: str, html_body: str | None = None
    ) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((settings.EMAIL_FROM_NAME, settings.EMAIL_FROM))
        msg["To"] = to
        msg.set_content(text_body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")

        if settings.SMTP_SSL:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT
            )
        else:
            server = smtplib.SMTP(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT
            )
        try:
            server.ehlo()
            if settings.SMTP_STARTTLS and not settings.SMTP_SSL:
                server.starttls()
                server.ehlo()
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 — closing errors must not mask send errors
                pass


@lru_cache
def get_email_sender() -> EmailSender:
    """The configured sender. Cached; tests override it via ``set_email_sender``."""
    if settings.EMAIL_BACKEND == "smtp":
        if not settings.SMTP_HOST:
            logger.error(
                "EMAIL_BACKEND=smtp but SMTP_HOST is empty -> falling back to the "
                "console backend. Set SMTP_HOST/PORT/USER/PASSWORD in .env."
            )
            return ConsoleEmailSender()
        return SmtpEmailSender()
    return ConsoleEmailSender()


# Test / runtime override hook. Kept explicit (rather than monkeypatching the
# lru_cache) so tests read clearly.
_override: EmailSender | None = None


def set_email_sender(sender: EmailSender | None) -> None:
    """Force a specific sender (used by the test suite). ``None`` restores the
    configured one."""
    global _override
    _override = sender


def _sender() -> EmailSender:
    return _override if _override is not None else get_email_sender()


def send_email(
    to: str, subject: str, text_body: str, html_body: str | None = None
) -> bool:
    """Send one message. Returns True on success, False on transport failure.

    Never raises: a mail-server hiccup must not turn into a 500 for the user,
    and must not change the response the client sees (see module docstring).
    """
    try:
        _sender().send(to, subject, text_body, html_body)
        return True
    except Exception as exc:  # noqa: BLE001 — see docstring
        logger.error(
            "Failed to send %r to %s: %s: %s",
            subject,
            to,
            type(exc).__name__,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Message templates
#
# Plain text is the source of truth (every client renders it); the HTML part is
# a light wrapper so it doesn't look like a 1995 mail in a modern client.
# ---------------------------------------------------------------------------


def _html_wrap(heading: str, paragraphs: list[str], button_label: str, link: str) -> str:
    body = "".join(f"<p style='margin:0 0 12px'>{p}</p>" for p in paragraphs)
    return (
        "<div style=\"font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        "font-size:15px;line-height:1.6;color:#1f2937;max-width:520px\">"
        f"<h2 style='margin:0 0 16px;font-size:19px'>{heading}</h2>"
        f"{body}"
        f"<p style='margin:24px 0'><a href='{link}' "
        "style=\"display:inline-block;background:#2563eb;color:#fff;"
        "padding:10px 20px;border-radius:6px;text-decoration:none\">"
        f"{button_label}</a></p>"
        "<p style='margin:0 0 12px;color:#6b7280;font-size:13px'>"
        "若按鈕無法點擊，請複製以下連結到瀏覽器開啟：<br>"
        f"<span style='word-break:break-all'>{link}</span></p>"
        "</div>"
    )


def verify_email_message(token: str, display_name: str | None) -> tuple[str, str, str]:
    """(subject, text_body, html_body) for the address-verification mail."""
    link = f"{settings.app_base_url}/?verify={token}"
    hours = max(1, settings.EMAIL_VERIFY_TTL_MINUTES // 60)
    who = (display_name or "").strip() or "你好"
    subject = "請驗證你的信箱 — Stock Quant"
    text = (
        f"{who}，\n\n"
        "感謝註冊 Stock Quant。請點擊以下連結驗證你的信箱：\n\n"
        f"{link}\n\n"
        f"這個連結在 {hours} 小時後失效。\n"
        "如果你沒有註冊過這個服務，請直接忽略這封信。\n"
    )
    html = _html_wrap(
        "請驗證你的信箱",
        [
            f"{who}，感謝註冊 Stock Quant。",
            f"請點擊下方按鈕完成信箱驗證。這個連結在 {hours} 小時後失效。",
            "如果你沒有註冊過這個服務，請直接忽略這封信。",
        ],
        "驗證信箱",
        link,
    )
    return subject, text, html


def password_reset_message(token: str, display_name: str | None) -> tuple[str, str, str]:
    """(subject, text_body, html_body) for the password-reset mail."""
    link = f"{settings.app_base_url}/?reset={token}"
    minutes = settings.PASSWORD_RESET_TTL_MINUTES
    who = (display_name or "").strip() or "你好"
    subject = "重設你的密碼 — Stock Quant"
    text = (
        f"{who}，\n\n"
        "我們收到了重設 Stock Quant 密碼的請求。請點擊以下連結設定新密碼：\n\n"
        f"{link}\n\n"
        f"這個連結在 {minutes} 分鐘後失效，且只能使用一次。\n"
        "如果不是你本人操作，請忽略這封信——你的密碼不會有任何變動。\n"
    )
    html = _html_wrap(
        "重設你的密碼",
        [
            f"{who}，我們收到了重設 Stock Quant 密碼的請求。",
            f"請點擊下方按鈕設定新密碼。連結在 {minutes} 分鐘後失效，且只能使用一次。",
            "如果不是你本人操作，請忽略這封信——你的密碼不會有任何變動。",
        ],
        "設定新密碼",
        link,
    )
    return subject, text, html

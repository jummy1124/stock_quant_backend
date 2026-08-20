"""Email verification and password-reset flows.

Runs entirely offline: the `outbox` fixture swaps in a capturing email sender,
so the tests read the real link out of the real message body rather than
reaching into the database for a token.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app.models import PURPOSE_PASSWORD_RESET, PURPOSE_VERIFY_EMAIL, EmailToken, User
from tests.conftest import register, token_from_email

GOOD_PW = "secret123"
NEW_PW = "brand-new-pw-9"


# ---------------------------------------------------------------------------
# Registration sends a verification email
# ---------------------------------------------------------------------------


def test_register_sends_verification_email(client, outbox):
    resp = register(client, email="v@example.com", display_name="Vee")
    assert resp.status_code == 201, resp.text

    assert len(outbox) == 1
    to, subject, text, html = outbox[0]
    assert to == "v@example.com"
    assert "驗證" in subject
    assert "?verify=" in text
    assert html and "?verify=" in html


def test_register_user_starts_unverified(client, outbox):
    resp = register(client, email="unv@example.com")
    assert resp.json()["user"]["email_verified"] is False


def test_unverified_user_can_still_use_the_app(client, outbox):
    """Verification is advisory: a bounced email must not lock anyone out."""
    resp = register(client, email="soft@example.com")
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}

    assert client.get("/userapi/me", headers=headers).status_code == 200
    put = client.put(
        "/userapi/records/TWSE/2330",
        headers=headers,
        json={"name": "TSMC", "market": "上市", "target_price": 1200.0},
    )
    assert put.status_code == 200


def test_email_is_normalised_to_lowercase(client, outbox):
    """Mixed-case signup must not create an account the user can't sign into."""
    resp = register(client, email="MiXeD@Example.COM", password=GOOD_PW)
    assert resp.status_code == 201
    assert resp.json()["user"]["email"] == "mixed@example.com"

    login = client.post(
        "/userapi/auth/login",
        json={"email": "mixed@EXAMPLE.com", "password": GOOD_PW},
    )
    assert login.status_code == 200


def test_duplicate_email_differing_only_in_case_is_rejected(client, outbox):
    assert register(client, email="dupe@example.com").status_code == 201
    assert register(client, email="DUPE@example.com").status_code == 409


# ---------------------------------------------------------------------------
# Verifying the address
# ---------------------------------------------------------------------------


def test_verify_email_marks_user_verified(client, outbox):
    register(client, email="vok@example.com")
    token = token_from_email(outbox[0][2], "verify")

    resp = client.post("/userapi/auth/verify-email", json={"token": token})
    assert resp.status_code == 200, resp.text
    assert resp.json()["email_verified"] is True
    assert resp.json()["email"] == "vok@example.com"


def test_verify_email_needs_no_authentication(client, outbox):
    """The link is routinely opened in a different browser."""
    register(client, email="other@example.com")
    token = token_from_email(outbox[0][2], "verify")
    # No Authorization header anywhere in this request.
    assert client.post("/userapi/auth/verify-email", json={"token": token}).status_code == 200


def test_me_reflects_verified_state(client, outbox):
    reg = register(client, email="refl@example.com")
    headers = {"Authorization": f"Bearer {reg.json()['token']}"}
    assert client.get("/userapi/me", headers=headers).json()["email_verified"] is False

    client.post(
        "/userapi/auth/verify-email",
        json={"token": token_from_email(outbox[0][2], "verify")},
    )
    assert client.get("/userapi/me", headers=headers).json()["email_verified"] is True


def test_verification_token_is_single_use(client, outbox):
    register(client, email="once@example.com")
    token = token_from_email(outbox[0][2], "verify")

    assert client.post("/userapi/auth/verify-email", json={"token": token}).status_code == 200
    second = client.post("/userapi/auth/verify-email", json={"token": token})
    assert second.status_code == 400


def test_unknown_verification_token_is_rejected(client, outbox):
    resp = client.post("/userapi/auth/verify-email", json={"token": "nope-not-a-token"})
    assert resp.status_code == 400


def test_expired_verification_token_is_rejected(client, outbox, engine):
    register(client, email="exp@example.com")
    token = token_from_email(outbox[0][2], "verify")

    with Session(engine) as s:
        row = s.exec(select(EmailToken)).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        s.add(row)
        s.commit()

    assert client.post("/userapi/auth/verify-email", json={"token": token}).status_code == 400


def test_resend_verification_invalidates_the_previous_link(client, outbox):
    """Only the newest link in the mailbox should work."""
    reg = register(client, email="resend@example.com")
    headers = {"Authorization": f"Bearer {reg.json()['token']}"}
    first = token_from_email(outbox[0][2], "verify")

    assert client.post("/userapi/auth/resend-verification", headers=headers).status_code == 202
    second = token_from_email(outbox[1][2], "verify")
    assert first != second

    assert client.post("/userapi/auth/verify-email", json={"token": first}).status_code == 400
    assert client.post("/userapi/auth/verify-email", json={"token": second}).status_code == 200


def test_resend_verification_requires_auth(client, outbox):
    """It must not be possible to aim our mail server at a stranger's inbox."""
    assert client.post("/userapi/auth/resend-verification").status_code == 401


def test_resend_verification_is_noop_when_already_verified(client, outbox):
    reg = register(client, email="already@example.com")
    headers = {"Authorization": f"Bearer {reg.json()['token']}"}
    client.post(
        "/userapi/auth/verify-email",
        json={"token": token_from_email(outbox[0][2], "verify")},
    )
    before = len(outbox)

    resp = client.post("/userapi/auth/resend-verification", headers=headers)
    assert resp.status_code == 202
    assert "already verified" in resp.json()["message"]
    assert len(outbox) == before  # no second email


# ---------------------------------------------------------------------------
# Forgot / reset password
# ---------------------------------------------------------------------------


def test_forgot_password_sends_reset_email(client, outbox):
    register(client, email="fp@example.com")
    outbox.clear()

    resp = client.post("/userapi/auth/forgot-password", json={"email": "fp@example.com"})
    assert resp.status_code == 202
    assert len(outbox) == 1
    assert "?reset=" in outbox[0][2]


def test_forgot_password_hides_whether_the_account_exists(client, outbox):
    """Same status AND same body for a registered and an unknown address."""
    register(client, email="known@example.com")
    outbox.clear()

    known = client.post("/userapi/auth/forgot-password", json={"email": "known@example.com"})
    unknown = client.post("/userapi/auth/forgot-password", json={"email": "ghost@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    # ...but only the real account actually gets mail.
    assert len(outbox) == 1
    assert outbox[0][0] == "known@example.com"


def test_reset_password_changes_the_password(client, outbox):
    register(client, email="rp@example.com", password=GOOD_PW)
    outbox.clear()
    client.post("/userapi/auth/forgot-password", json={"email": "rp@example.com"})
    token = token_from_email(outbox[0][2], "reset")

    resp = client.post(
        "/userapi/auth/reset-password", json={"token": token, "password": NEW_PW}
    )
    assert resp.status_code == 200, resp.text

    assert client.post(
        "/userapi/auth/login", json={"email": "rp@example.com", "password": GOOD_PW}
    ).status_code == 401
    assert client.post(
        "/userapi/auth/login", json={"email": "rp@example.com", "password": NEW_PW}
    ).status_code == 200


def test_reset_password_signs_the_user_in(client, outbox):
    register(client, email="rpin@example.com", password=GOOD_PW)
    outbox.clear()
    client.post("/userapi/auth/forgot-password", json={"email": "rpin@example.com"})
    token = token_from_email(outbox[0][2], "reset")

    resp = client.post(
        "/userapi/auth/reset-password", json={"token": token, "password": NEW_PW}
    )
    new_token = resp.json()["token"]
    me = client.get("/userapi/me", headers={"Authorization": f"Bearer {new_token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "rpin@example.com"


def test_reset_password_invalidates_tokens_issued_earlier(client, outbox):
    """The point of resetting a password you think was stolen: kill the session
    that was using it."""
    reg = register(client, email="kick@example.com", password=GOOD_PW)
    old_headers = {"Authorization": f"Bearer {reg.json()['token']}"}
    assert client.get("/userapi/me", headers=old_headers).status_code == 200

    outbox.clear()
    client.post("/userapi/auth/forgot-password", json={"email": "kick@example.com"})
    token = token_from_email(outbox[0][2], "reset")
    client.post("/userapi/auth/reset-password", json={"token": token, "password": NEW_PW})

    assert client.get("/userapi/me", headers=old_headers).status_code == 401


def test_reset_password_also_verifies_the_address(client, outbox):
    """Clicking a link we mailed there is proof of control."""
    register(client, email="rv@example.com", password=GOOD_PW)
    outbox.clear()
    client.post("/userapi/auth/forgot-password", json={"email": "rv@example.com"})
    token = token_from_email(outbox[0][2], "reset")

    resp = client.post(
        "/userapi/auth/reset-password", json={"token": token, "password": NEW_PW}
    )
    assert resp.json()["user"]["email_verified"] is True


def test_reset_token_is_single_use(client, outbox):
    register(client, email="ru@example.com", password=GOOD_PW)
    outbox.clear()
    client.post("/userapi/auth/forgot-password", json={"email": "ru@example.com"})
    token = token_from_email(outbox[0][2], "reset")

    assert client.post(
        "/userapi/auth/reset-password", json={"token": token, "password": NEW_PW}
    ).status_code == 200
    assert client.post(
        "/userapi/auth/reset-password", json={"token": token, "password": "another-pw-1"}
    ).status_code == 400


def test_requesting_a_second_reset_kills_the_first_link(client, outbox):
    register(client, email="two@example.com", password=GOOD_PW)
    outbox.clear()
    client.post("/userapi/auth/forgot-password", json={"email": "two@example.com"})
    first = token_from_email(outbox[0][2], "reset")
    client.post("/userapi/auth/forgot-password", json={"email": "two@example.com"})
    second = token_from_email(outbox[1][2], "reset")

    assert client.post(
        "/userapi/auth/reset-password", json={"token": first, "password": NEW_PW}
    ).status_code == 400
    assert client.post(
        "/userapi/auth/reset-password", json={"token": second, "password": NEW_PW}
    ).status_code == 200


def test_expired_reset_token_is_rejected(client, outbox, engine):
    register(client, email="rexp@example.com", password=GOOD_PW)
    outbox.clear()
    client.post("/userapi/auth/forgot-password", json={"email": "rexp@example.com"})
    token = token_from_email(outbox[0][2], "reset")

    with Session(engine) as s:
        row = s.exec(
            select(EmailToken).where(EmailToken.purpose == PURPOSE_PASSWORD_RESET)
        ).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        s.add(row)
        s.commit()

    assert client.post(
        "/userapi/auth/reset-password", json={"token": token, "password": NEW_PW}
    ).status_code == 400


# ---------------------------------------------------------------------------
# Tokens must not be usable across purposes
# ---------------------------------------------------------------------------


def test_verification_token_cannot_reset_a_password(client, outbox):
    """Otherwise a signup email would double as a password-reset link."""
    register(client, email="cross1@example.com", password=GOOD_PW)
    verify_token = token_from_email(outbox[0][2], "verify")

    resp = client.post(
        "/userapi/auth/reset-password",
        json={"token": verify_token, "password": NEW_PW},
    )
    assert resp.status_code == 400
    # The original password still works.
    assert client.post(
        "/userapi/auth/login", json={"email": "cross1@example.com", "password": GOOD_PW}
    ).status_code == 200


def test_reset_token_cannot_verify_an_email(client, outbox):
    register(client, email="cross2@example.com", password=GOOD_PW)
    outbox.clear()
    client.post("/userapi/auth/forgot-password", json={"email": "cross2@example.com"})
    reset_token = token_from_email(outbox[0][2], "reset")

    assert client.post(
        "/userapi/auth/verify-email", json={"token": reset_token}
    ).status_code == 400


# ---------------------------------------------------------------------------
# Tokens are never stored in usable form
# ---------------------------------------------------------------------------


def test_raw_token_is_not_persisted(client, outbox, engine):
    """A database dump must not let an attacker replay the link."""
    register(client, email="hash@example.com")
    raw = token_from_email(outbox[0][2], "verify")

    with Session(engine) as s:
        row = s.exec(select(EmailToken)).one()

    assert row.token_hash != raw
    assert raw not in row.token_hash
    assert len(row.token_hash) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("password", ["", "a", "short7c"])
def test_register_rejects_weak_passwords(client, outbox, password):
    resp = client.post(
        "/userapi/auth/register", json={"email": "weak@example.com", "password": password}
    )
    assert resp.status_code == 422


def test_register_rejects_password_over_bcrypt_limit(client, outbox):
    """bcrypt silently truncates past 72 bytes; reject rather than mislead."""
    resp = client.post(
        "/userapi/auth/register",
        json={"email": "long@example.com", "password": "x" * 73},
    )
    assert resp.status_code == 422


def test_reset_rejects_weak_passwords(client, outbox):
    register(client, email="weakreset@example.com", password=GOOD_PW)
    outbox.clear()
    client.post("/userapi/auth/forgot-password", json={"email": "weakreset@example.com"})
    token = token_from_email(outbox[0][2], "reset")

    assert client.post(
        "/userapi/auth/reset-password", json={"token": token, "password": "short"}
    ).status_code == 422


def test_login_still_accepts_a_legacy_short_password(client, outbox, engine):
    """Accounts created before the policy existed must keep working."""
    from app.security import hash_password

    with Session(engine) as s:
        s.add(
            User(
                id=uuid.uuid4(),
                email="legacy@example.com",
                password_hash=hash_password("old"),
            )
        )
        s.commit()

    resp = client.post(
        "/userapi/auth/login", json={"email": "legacy@example.com", "password": "old"}
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Token bookkeeping
# ---------------------------------------------------------------------------


def test_consumed_token_row_is_marked_used_not_deleted(client, outbox, engine):
    """Keeping the row (with used_at) preserves an audit trail and makes replay
    attempts distinguishable from unknown tokens in the logs."""
    register(client, email="audit@example.com")
    token = token_from_email(outbox[0][2], "verify")
    client.post("/userapi/auth/verify-email", json={"token": token})

    with Session(engine) as s:
        row = s.exec(
            select(EmailToken).where(EmailToken.purpose == PURPOSE_VERIFY_EMAIL)
        ).one()
    assert row.used_at is not None

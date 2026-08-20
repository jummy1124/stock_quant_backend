"""Rate limiting on the abuse-prone auth endpoints.

The limiter is process-global, so these tests enable it explicitly, reset it
between cases, and restore the previous setting afterwards — otherwise counters
would leak into the rest of the suite (which runs with limits off).
"""
import pytest

from app.config import settings
from app.ratelimit import RULES, limiter
from tests.conftest import register

PW = "secret123"


@pytest.fixture(name="limits_on")
def limits_on_fixture():
    previous = settings.RATE_LIMIT_ENABLED
    settings.RATE_LIMIT_ENABLED = True
    limiter.reset()
    yield
    limiter.reset()
    settings.RATE_LIMIT_ENABLED = previous


def test_login_is_rate_limited(client, outbox, limits_on):
    register(client, email="rl@example.com", password=PW)
    limiter.reset()  # registration consumed a register_ip slot

    limit = RULES["login_ip"].limit
    for _ in range(limit):
        resp = client.post(
            "/userapi/auth/login", json={"email": "rl@example.com", "password": "wrong"}
        )
        assert resp.status_code == 401

    blocked = client.post(
        "/userapi/auth/login", json={"email": "rl@example.com", "password": "wrong"}
    )
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_rate_limited_login_blocks_even_the_correct_password(client, outbox, limits_on):
    """Otherwise an attacker learns they found the password from the 200."""
    register(client, email="rl2@example.com", password=PW)
    limiter.reset()

    for _ in range(RULES["login_ip"].limit):
        client.post(
            "/userapi/auth/login", json={"email": "rl2@example.com", "password": "wrong"}
        )

    resp = client.post(
        "/userapi/auth/login", json={"email": "rl2@example.com", "password": PW}
    )
    assert resp.status_code == 429


def test_forgot_password_is_rate_limited_per_address(client, outbox, limits_on):
    register(client, email="rl3@example.com", password=PW)
    limiter.reset()
    outbox.clear()

    limit = RULES["forgot_email"].limit
    for _ in range(limit):
        resp = client.post(
            "/userapi/auth/forgot-password", json={"email": "rl3@example.com"}
        )
        assert resp.status_code == 202

    blocked = client.post(
        "/userapi/auth/forgot-password", json={"email": "rl3@example.com"}
    )
    assert blocked.status_code == 429
    # The mailbox got `limit` messages, not limit+1.
    assert len(outbox) == limit


def test_forgot_password_limit_applies_to_unknown_addresses_too(
    client, outbox, limits_on
):
    """If unknown addresses were exempt, the 429 boundary would itself reveal
    which addresses are registered."""
    limit = RULES["forgot_email"].limit
    for _ in range(limit):
        assert client.post(
            "/userapi/auth/forgot-password", json={"email": "ghost@example.com"}
        ).status_code == 202

    assert client.post(
        "/userapi/auth/forgot-password", json={"email": "ghost@example.com"}
    ).status_code == 429


def test_register_is_rate_limited(client, outbox, limits_on):
    limit = RULES["register_ip"].limit
    for i in range(limit):
        assert register(client, email=f"bulk{i}@example.com", password=PW).status_code == 201

    assert register(client, email="bulk-over@example.com", password=PW).status_code == 429


def test_resend_verification_is_rate_limited(client, outbox, limits_on):
    reg = register(client, email="rlresend@example.com", password=PW)
    headers = {"Authorization": f"Bearer {reg.json()['token']}"}
    limiter.reset()

    limit = RULES["resend_user"].limit
    for _ in range(limit):
        assert client.post(
            "/userapi/auth/resend-verification", headers=headers
        ).status_code == 202

    assert client.post(
        "/userapi/auth/resend-verification", headers=headers
    ).status_code == 429


def test_limits_are_per_key_not_global(client, outbox, limits_on):
    """One address being throttled must not throttle everyone else."""
    for _ in range(RULES["forgot_email"].limit):
        client.post("/userapi/auth/forgot-password", json={"email": "noisy@example.com"})
    assert client.post(
        "/userapi/auth/forgot-password", json={"email": "noisy@example.com"}
    ).status_code == 429

    assert client.post(
        "/userapi/auth/forgot-password", json={"email": "quiet@example.com"}
    ).status_code == 202


def test_forwarded_for_cannot_be_spoofed_to_reset_the_counter(
    client, outbox, limits_on
):
    """nginx APPENDS the peer address to X-Forwarded-For, so the real client is
    the last entry. A client that prepends fake hops must not get a fresh
    bucket each time."""
    limit = RULES["register_ip"].limit
    for i in range(limit):
        resp = client.post(
            "/userapi/auth/register",
            json={"email": f"spoof{i}@example.com", "password": PW},
            headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.9"},
        )
        assert resp.status_code == 201

    blocked = client.post(
        "/userapi/auth/register",
        json={"email": "spoof-last@example.com", "password": PW},
        headers={"X-Forwarded-For": "10.0.0.99, 203.0.113.9"},
    )
    assert blocked.status_code == 429


def test_limits_are_off_when_disabled(client, outbox):
    """The rest of the suite relies on this."""
    assert settings.RATE_LIMIT_ENABLED is False
    for _ in range(RULES["login_ip"].limit + 3):
        resp = client.post(
            "/userapi/auth/login", json={"email": "nobody@example.com", "password": PW}
        )
        assert resp.status_code == 401

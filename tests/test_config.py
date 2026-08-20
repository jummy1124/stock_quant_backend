"""Configuration guard rails.

The point of these is that a misconfigured deploy must crash at startup rather
than come up quietly with a signing key an attacker already knows.
"""
import pytest
from pydantic import ValidationError

from app.config import Settings


def _settings(**overrides):
    # _env_file=None so a developer's real .env can't leak into the assertions.
    return Settings(_env_file=None, **overrides)


def test_jwt_secret_is_required(monkeypatch):
    # conftest exports JWT_SECRET for the rest of the suite; drop it here so we
    # are testing the "nothing configured at all" case a fresh deploy hits.
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValidationError):
        _settings()


@pytest.mark.parametrize("secret", ["", "short", "change-me", "x" * 31])
def test_weak_jwt_secrets_are_rejected(secret):
    with pytest.raises(ValidationError):
        _settings(JWT_SECRET=secret)


def test_long_random_jwt_secret_is_accepted():
    s = _settings(JWT_SECRET="k" * 32)
    assert s.JWT_SECRET == "k" * 32


def test_email_backend_must_be_known():
    with pytest.raises(ValidationError):
        _settings(JWT_SECRET="k" * 32, EMAIL_BACKEND="carrier-pigeon")


def test_email_backend_is_case_insensitive():
    assert _settings(JWT_SECRET="k" * 32, EMAIL_BACKEND="SMTP").EMAIL_BACKEND == "smtp"


def test_app_base_url_strips_trailing_slash():
    """Links are built as f"{app_base_url}/?verify=..." — a trailing slash in
    the setting would produce `//?verify=`."""
    s = _settings(JWT_SECRET="k" * 32, APP_BASE_URL="https://example.com/")
    assert s.app_base_url == "https://example.com"


def test_allowed_origins_default_is_not_a_wildcard():
    assert _settings(JWT_SECRET="k" * 32).allowed_origins_list != ["*"]


def test_allowed_origins_parses_a_list():
    s = _settings(
        JWT_SECRET="k" * 32, ALLOWED_ORIGINS="https://a.example, https://b.example"
    )
    assert s.allowed_origins_list == ["https://a.example", "https://b.example"]

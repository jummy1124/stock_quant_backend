import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlmodel import Session

from app.config import settings
from app.db import get_session
from app.models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# auto_error=False so we can raise our own 401 (with WWW-Authenticate) for any
# missing / malformed / expired token, per the API contract.
_bearer = HTTPBearer(auto_error=False)

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_access_token(user_id: uuid.UUID, token_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        # Session generation. Bumped by a password change; see User.token_version.
        "ver": int(token_version),
        "iat": now,
        # Random id per token. Nothing consumes it yet; it exists so a future
        # per-token revocation list has a stable handle on an individual token.
        "jti": secrets.token_urlsafe(12),
        "exp": now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def token_for(user: User) -> str:
    """Access token carrying the user's current session generation."""
    return create_access_token(user.id, user.token_version)


# ---------------------------------------------------------------------------
# Single-use email tokens (verification / password reset)
# ---------------------------------------------------------------------------


def generate_email_token() -> str:
    """A fresh, URL-safe, 256-bit token. Returned raw — it goes in the email
    and is never stored in this form."""
    return secrets.token_urlsafe(32)


def hash_email_token(raw_token: str) -> str:
    """SHA-256 hex digest, which is what the database stores.

    Deterministic (unlike bcrypt) so the token can be looked up by index, and
    fast — safe here because the input is high-entropy random, not a password.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Current-user dependency
# ---------------------------------------------------------------------------


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: Session = Depends(get_session),
) -> User:
    if credentials is None or not credentials.credentials:
        raise _CREDENTIALS_EXCEPTION

    token = credentials.credentials
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.PyJWTError:
        raise _CREDENTIALS_EXCEPTION

    sub = payload.get("sub")
    if not sub:
        raise _CREDENTIALS_EXCEPTION

    try:
        user_id = uuid.UUID(str(sub))
    except (ValueError, TypeError):
        raise _CREDENTIALS_EXCEPTION

    user = session.get(User, user_id)
    if user is None:
        raise _CREDENTIALS_EXCEPTION

    # Session generation must match. A password change bumps the stored
    # version, so every token minted before it — including one an attacker is
    # holding — stops validating here. Tokens from an older build of this
    # service carry no `ver`; those are treated as stale rather than trusted,
    # so a password reset cannot be outlived by one.
    if payload.get("ver") is None or int(payload["ver"]) != user.token_version:
        raise _CREDENTIALS_EXCEPTION

    return user

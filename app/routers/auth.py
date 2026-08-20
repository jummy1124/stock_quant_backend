"""Account endpoints: register, login, email verification, password reset.

Two design rules run through this module.

**Anti-enumeration.** ``/auth/forgot-password`` answers ``202`` with the same
body for every syntactically valid address — registered or not, mail sent or
not. Anything else (a 404, a different message, a visibly different latency)
turns the endpoint into an oracle for "does this person have an account here".
The same reasoning is why token failures are one undifferentiated 400 rather
than separate "expired" / "already used" / "no such token" answers.

**Verification is advisory.** A newly registered user gets a token immediately
and can use the whole app; the UI shows a banner until the address is confirmed.
So a bounced or delayed verification email is a nuisance, never a lockout.
"""
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from sqlmodel import Session

from app import crud
from app.config import settings
from app.db import get_session
from app.email import (
    password_reset_message,
    send_email,
    verify_email_message,
)
from app.models import PURPOSE_PASSWORD_RESET, PURPOSE_VERIFY_EMAIL, User
from app.ratelimit import client_ip, enforce
from app.schemas import (
    AuthResponse,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResetPasswordRequest,
    UserOut,
    VerifyEmailRequest,
)
from app.security import (
    generate_email_token,
    get_current_user,
    hash_email_token,
    hash_password,
    token_for,
    verify_password,
)

router = APIRouter(prefix="/userapi", tags=["auth"])

# One message for every outcome of a mail-sending request. See module docstring.
_GENERIC_EMAIL_ACK = (
    "If that address has an account, we've sent an email with the next steps."
)

_INVALID_TOKEN = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="This link is invalid or has expired. Please request a new one.",
)


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        email_verified=user.email_verified,
    )


def _issue_verification_email(
    session: Session, user: User, background: BackgroundTasks
) -> None:
    """Mint a fresh verification token and queue the email.

    Any previously issued verification token is retired first, so a mailbox with
    three "verify your email" messages in it only has one working link — the
    newest. Sending happens in a background task: SMTP round-trips are slow and
    the client has no reason to wait for one.
    """
    crud.invalidate_email_tokens(session, user.id, PURPOSE_VERIFY_EMAIL)
    raw = generate_email_token()
    crud.create_email_token(
        session,
        user_id=user.id,
        purpose=PURPOSE_VERIFY_EMAIL,
        token_hash=hash_email_token(raw),
        ttl_minutes=settings.EMAIL_VERIFY_TTL_MINUTES,
    )
    subject, text, html = verify_email_message(raw, user.display_name)
    background.add_task(send_email, user.email, subject, text, html)


# ---------------------------------------------------------------------------
# Register / login / logout / me
# ---------------------------------------------------------------------------


@router.post(
    "/auth/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    body: RegisterRequest,
    background: BackgroundTasks,
    request: Request,
    session: Session = Depends(get_session),
):
    enforce("register_ip", client_ip(request))

    email = crud.normalize_email(body.email)
    if crud.get_user_by_email(session, email) is not None:
        # 409 here is a deliberate exception to the anti-enumeration rule: a
        # signup form cannot function without telling the user the address is
        # taken, and the register endpoint is rate-limited per IP for that
        # reason. Sign-in and password reset stay opaque.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )

    user = crud.create_user(
        session,
        email=email,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    _issue_verification_email(session, user, background)

    token = token_for(user)
    return AuthResponse(token=token, user=_user_out(user))


@router.post("/auth/login", response_model=AuthResponse)
def login(
    body: LoginRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    email = crud.normalize_email(body.email)
    # Two independent counters: the IP one stops a single attacker hammering
    # many accounts, the email one stops a distributed attempt on one account.
    enforce("login_ip", client_ip(request))
    enforce("login_email", email)

    user = crud.get_user_by_email(session, email)
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    token = token_for(user)
    return AuthResponse(token=token, user=_user_out(user))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout():
    # Stateless JWT: client simply discards the token. No-op server side.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return _user_out(current_user)


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


@router.post("/auth/verify-email", response_model=UserOut)
def verify_email(
    body: VerifyEmailRequest,
    session: Session = Depends(get_session),
):
    """Confirm an address from the emailed link. No authentication required —
    the link is often opened in a different browser from the one that signed up,
    and possession of the token *is* the proof."""
    token = crud.get_usable_email_token(
        session, PURPOSE_VERIFY_EMAIL, hash_email_token(body.token)
    )
    if token is None:
        raise _INVALID_TOKEN

    user = session.get(User, token.user_id)
    if user is None:
        raise _INVALID_TOKEN

    crud.consume_email_token(session, token)
    user = crud.mark_email_verified(session, user)
    return _user_out(user)


@router.post(
    "/auth/resend-verification",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def resend_verification(
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Send a new verification email to the signed-in user's own address.

    Authenticated (rather than taking an address in the body) so it cannot be
    pointed at a stranger's inbox.
    """
    enforce("resend_user", str(current_user.id))

    if current_user.email_verified:
        return MessageResponse(message="This address is already verified.")

    _issue_verification_email(session, current_user, background)
    return MessageResponse(message="Verification email sent.")


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


@router.post(
    "/auth/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def forgot_password(
    body: ForgotPasswordRequest,
    background: BackgroundTasks,
    request: Request,
    session: Session = Depends(get_session),
):
    email = crud.normalize_email(body.email)
    enforce("forgot_ip", client_ip(request))
    enforce("forgot_email", email)

    user = crud.get_user_by_email(session, email)
    if user is not None:
        # Retire any outstanding reset link before issuing a new one, so
        # "I clicked request three times" leaves exactly one working link.
        crud.invalidate_email_tokens(session, user.id, PURPOSE_PASSWORD_RESET)
        raw = generate_email_token()
        crud.create_email_token(
            session,
            user_id=user.id,
            purpose=PURPOSE_PASSWORD_RESET,
            token_hash=hash_email_token(raw),
            ttl_minutes=settings.PASSWORD_RESET_TTL_MINUTES,
        )
        subject, text, html = password_reset_message(raw, user.display_name)
        background.add_task(send_email, user.email, subject, text, html)

    # Same answer either way. See module docstring.
    return MessageResponse(message=_GENERIC_EMAIL_ACK)


@router.post("/auth/reset-password", response_model=AuthResponse)
def reset_password(
    body: ResetPasswordRequest,
    session: Session = Depends(get_session),
):
    """Set a new password using an emailed token, and sign the user in.

    Three things happen together, and all three matter:

    1. The token is consumed, and every other outstanding reset token for the
       account is retired.
    2. ``password_changed_at`` is bumped, which invalidates every JWT issued
       before this moment — so if the reset was triggered because a session was
       compromised, that session dies here.
    3. The address is marked verified: clicking a link we mailed there is proof
       of control, so making the user do it twice would be theatre.

    The freshly minted token in the response is issued *after* the bump, so the
    user stays signed in on this device.
    """
    token = crud.get_usable_email_token(
        session, PURPOSE_PASSWORD_RESET, hash_email_token(body.token)
    )
    if token is None:
        raise _INVALID_TOKEN

    user = session.get(User, token.user_id)
    if user is None:
        raise _INVALID_TOKEN

    crud.consume_email_token(session, token)
    crud.invalidate_email_tokens(session, user.id, PURPOSE_PASSWORD_RESET)
    user = crud.set_password(session, user, hash_password(body.password))
    user = crud.mark_email_verified(session, user)

    return AuthResponse(token=token_for(user), user=_user_out(user))

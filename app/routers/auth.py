from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlmodel import Session

from app import crud
from app.db import get_session
from app.models import User
from app.schemas import AuthResponse, LoginRequest, RegisterRequest, UserOut
from app.security import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/userapi", tags=["auth"])


def _user_out(user: User) -> UserOut:
    return UserOut(id=str(user.id), email=user.email, display_name=user.display_name)


@router.post(
    "/auth/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(body: RegisterRequest, session: Session = Depends(get_session)):
    existing = crud.get_user_by_email(session, body.email)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )
    user = crud.create_user(
        session,
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    token = create_access_token(user.id)
    return AuthResponse(token=token, user=_user_out(user))


@router.post("/auth/login", response_model=AuthResponse)
def login(body: LoginRequest, session: Session = Depends(get_session)):
    user = crud.get_user_by_email(session, body.email)
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    token = create_access_token(user.id)
    return AuthResponse(token=token, user=_user_out(user))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout():
    # Stateless JWT: client simply discards the token. No-op server side.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return _user_out(current_user)

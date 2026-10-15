"""Authentication endpoints: register, login and profile."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from ..config import get_settings
from ..dependencies import CurrentUser, DbSession
from ..models import User
from ..schemas import TokenOut, UserCreate, UserLogin, UserOut
from ..security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: DbSession) -> TokenOut:
    """Create an account and return a ready-to-use access token."""
    email = payload.email.lower().strip()
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(status_code=409, detail="An account with that email already exists")

    # On a fresh deployment the first account becomes the administrator so
    # there is always someone able to manage nodes and the model. Accounts in
    # ADMIN_EMAILS are promoted too.
    is_first_user = (db.scalar(select(func.count(User.id))) or 0) == 0
    user = User(
        email=email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        is_admin=is_first_user or email in settings.admin_email_list,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return TokenOut(
        access_token=create_access_token(user.id),
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.post("/login", response_model=TokenOut)
def login(payload: UserLogin, db: DbSession) -> TokenOut:
    """Exchange credentials for a JWT."""
    email = payload.email.lower().strip()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    return TokenOut(
        access_token=create_access_token(user.id),
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    """Return the authenticated user."""
    return UserOut.model_validate(user)

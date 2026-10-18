"""Authentication endpoints.

A deliberately small surface: register, sign in (by password, or by a phone
number or Google account that Firebase verified), read the current account, and — in mock mode
only — a password-less demo sign-in so the local demo needs no credentials.
There is no password-reset or email-verification flow; see
`docs/SVARAH_SECURITY_AND_DEPLOYMENT.md` for what a production deployment still
has to add.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import current_user
from ..errors import AuthenticationError, AuthUnavailableError, ConflictError, ValidationError
from ..models import User, utcnow
from ..security import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    mint_token,
    normalize_email,
    owner_uid_for_email,
    verify_password,
)
from ..services.firebase_auth import verify_google_id_token, verify_phone_id_token

router = APIRouter(prefix="/auth", tags=["auth"])

DEMO_EMAIL = "demo@svarah.local"
DEMO_NAME = "Demo Shopkeeper"

#: `users.email` is required and unique, so an account created by phone
#: sign-in gets a placeholder under this reserved (never-resolvable) domain.
#: Registration refuses the domain, so the placeholder cannot be squatted.
PHONE_EMAIL_DOMAIN = "phone.svarah.invalid"


#: Deliberately permissive: enough structure to catch a typo, without pulling
#: in `email-validator` (and without rejecting a valid-but-unusual address).
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


class _EmailMixin(BaseModel):
    email: str = Field(min_length=3, max_length=255)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        value = value.strip()
        if not _EMAIL_PATTERN.match(value):
            raise ValueError("Enter a valid email address.")
        return value


class RegisterRequest(_EmailMixin):
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)
    display_name: str | None = Field(default=None, max_length=128)


class LoginRequest(_EmailMixin):
    password: str = Field(min_length=1, max_length=256)


class PhoneLoginRequest(BaseModel):
    #: The Firebase ID token the browser received after the SMS code was confirmed.
    id_token: str = Field(min_length=20, max_length=8192)
    display_name: str | None = Field(default=None, max_length=128)


class GoogleLoginRequest(BaseModel):
    #: The Firebase ID token the browser received from Google's sign-in window.
    id_token: str = Field(min_length=20, max_length=8192)


class AccountView(BaseModel):
    owner_uid: str
    email: str
    display_name: str
    is_demo: bool
    #: Set for accounts that sign in by phone; `email` is then a placeholder.
    phone: str | None = None


class SessionView(BaseModel):
    token: str
    expires_at: datetime
    account: AccountView
    #: True when the signing key was generated for this process only, so the
    #: UI can warn that sessions end at the next backend restart.
    ephemeral_signing_key: bool


def _account(user: User) -> AccountView:
    return AccountView(
        owner_uid=user.owner_uid,
        email=user.email,
        display_name=user.display_name,
        is_demo=user.is_demo,
        phone=user.phone,
    )


def _session(user: User) -> SessionView:
    settings = get_settings()
    token, expires = mint_token(uid=user.owner_uid, email=user.email, settings=settings)
    return SessionView(
        token=token,
        expires_at=datetime.fromtimestamp(expires, tz=timezone.utc),
        account=_account(user),
        ephemeral_signing_key=settings.auth_secret_ephemeral,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=SessionView)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> SessionView:
    settings = get_settings()
    if not settings.auth_configured:
        raise AuthUnavailableError(
            "Authentication is not configured: set TITAN_AUTH_SECRET.",
            details={"configured": False},
        )
    email = normalize_email(str(payload.email))
    if email.endswith(f"@{PHONE_EMAIL_DOMAIN}"):
        raise ValidationError("Enter a valid email address.", code="email_reserved")
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise ConflictError(
            "An account already exists for this email.", code="email_taken"
        )
    if not payload.password.strip():
        raise ValidationError("The password cannot be blank.", code="password_blank")
    user = User(
        owner_uid=owner_uid_for_email(email),
        email=email,
        display_name=(payload.display_name or "").strip() or email.split("@", 1)[0],
        password_hash=hash_password(payload.password),
        is_demo=False,
        last_login_at=utcnow(),
    )
    db.add(user)
    db.commit()
    return _session(user)


@router.post("/login", response_model=SessionView)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> SessionView:
    email = normalize_email(str(payload.email))
    user = db.scalar(select(User).where(User.email == email))
    # One message for "unknown email" and "wrong password" alike: the response
    # must not reveal whether an address is registered.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AuthenticationError(
            "That email and password combination is not valid.",
            code="invalid_credentials",
        )
    user.last_login_at = utcnow()
    db.commit()
    return _session(user)


@router.post("/phone", response_model=SessionView)
def phone_login(payload: PhoneLoginRequest, db: Session = Depends(get_db)) -> SessionView:
    """Sign in with a phone number that Firebase has verified by SMS.

    The first sign-in from a number creates its account; later ones return the
    same account. The owner UID comes from the Firebase account id, so it stays
    stable even if the owner later changes their number in Firebase.
    """
    settings = get_settings()
    if not settings.auth_configured:
        raise AuthUnavailableError(
            "Authentication is not configured: set TITAN_AUTH_SECRET.",
            details={"configured": False},
        )
    identity = verify_phone_id_token(payload.id_token, settings)
    owner_uid = f"fb_{identity.uid}"
    user = db.scalar(select(User).where(User.owner_uid == owner_uid))
    if user is None:
        user = User(
            owner_uid=owner_uid,
            email=f"{identity.phone.lstrip('+')}@{PHONE_EMAIL_DOMAIN}",
            display_name=(payload.display_name or "").strip() or identity.phone,
            password_hash=None,
            phone=identity.phone,
            is_demo=False,
        )
        db.add(user)
    else:
        user.phone = identity.phone
    user.last_login_at = utcnow()
    db.commit()
    return _session(user)


@router.post("/google", response_model=SessionView)
def google_login(payload: GoogleLoginRequest, db: Session = Depends(get_db)) -> SessionView:
    """Sign in with a Google account that Firebase has verified.

    The first sign-in creates the account. An account that already exists for
    the same email address (Google has verified the address) is reused, so the
    owner keeps their campaigns.
    """
    settings = get_settings()
    if not settings.auth_configured:
        raise AuthUnavailableError(
            "Authentication is not configured: set TITAN_AUTH_SECRET.",
            details={"configured": False},
        )
    identity = verify_google_id_token(payload.id_token, settings)
    email = normalize_email(identity.email)
    owner_uid = f"fb_{identity.uid}"
    user = db.scalar(select(User).where(User.owner_uid == owner_uid))
    if user is None:
        user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            owner_uid=owner_uid,
            email=email,
            display_name=identity.name or email.split("@", 1)[0],
            password_hash=None,
            is_demo=False,
        )
        db.add(user)
    user.last_login_at = utcnow()
    db.commit()
    return _session(user)


@router.post("/demo", response_model=SessionView)
def demo_login(db: Session = Depends(get_db)) -> SessionView:
    """Password-less sign-in for the offline demo. Mock mode only.

    This is a convenience, not a security model: it is refused outright in live
    mode and whenever `TITAN_ALLOW_DEMO_LOGIN=false`.
    """
    settings = get_settings()
    if not settings.demo_login_enabled:
        raise ConflictError(
            "Demo sign-in is disabled. It is available only when TITAN_MODE=mock "
            "and TITAN_ALLOW_DEMO_LOGIN is not false.",
            code="demo_login_disabled",
            details={"mode": settings.titan_mode},
        )
    user = db.scalar(select(User).where(User.email == DEMO_EMAIL))
    if user is None:
        user = User(
            owner_uid=owner_uid_for_email(DEMO_EMAIL),
            email=DEMO_EMAIL,
            display_name=DEMO_NAME,
            password_hash=None,
            is_demo=True,
        )
        db.add(user)
    user.last_login_at = utcnow()
    db.commit()
    return _session(user)


@router.post("/refresh", response_model=SessionView)
def refresh(user: User = Depends(current_user)) -> SessionView:
    """Trade a still-valid session for a fresh one.

    The website calls this each time it opens, so a session keeps sliding
    forward and only ends when the owner signs out (or stays away for the
    whole of `TITAN_AUTH_TOKEN_TTL_HOURS`).
    """
    return _session(user)


@router.get("/me", response_model=AccountView)
def me(user: User = Depends(current_user)) -> AccountView:
    return _account(user)

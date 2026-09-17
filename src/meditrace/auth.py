"""JWT bearer authentication, roles, and account lockout.

There is no public self-registration endpoint: opening one on an internet
facing deployment of a health-data app is an easy way to let strangers create
accounts. Operators create the first account with
``python -m src.meditrace.create_user``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .crypto import hash_password, verify_password
from .models import User

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = 30
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

ROLE_ADMIN = "admin"
ROLE_CLINICIAN = "clinician"
ROLE_REVIEWER = "reviewer"
ROLES = {ROLE_ADMIN, ROLE_CLINICIAN, ROLE_REVIEWER}

_bearer = HTTPBearer(auto_error=False)


def _secret_key() -> str:
    key = os.getenv("SECRET_KEY")
    if key:
        return key
    if os.getenv("VERCEL") == "1":
        raise RuntimeError(
            "SECRET_KEY is not set. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"` "
            "and set it as a Vercel environment variable before deploying."
        )
    # Local/dev fallback only: ephemeral, invalidates sessions on restart.
    global _dev_secret
    try:
        return _dev_secret
    except NameError:
        _dev_secret = os.urandom(32).hex()
        return _dev_secret


def create_access_token(user: "User") -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_MINUTES),
    }
    return jwt.encode(payload, _secret_key(), algorithm=JWT_ALGORITHM)


def create_user(session: Session, email: str, password: str, role: str) -> User:
    if role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}")
    if session.scalar(select(User).where(User.email == email)):
        raise ValueError("A user with this email already exists")
    user = User(email=email, hashed_password=hash_password(password), role=role)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def authenticate(session: Session, email: str, password: str) -> User:
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        # Run a hash comparison anyway so login timing doesn't reveal whether
        # the email exists.
        verify_password(password, hash_password("decoy-password"))
        raise HTTPException(401, "Invalid email or password")
    now = datetime.now(timezone.utc)
    if user.locked_until and user.locked_until > now:
        raise HTTPException(
            423, f"Account locked until {user.locked_until.isoformat()}"
        )
    if not verify_password(password, user.hashed_password):
        user.failed_attempts += 1
        if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_attempts = 0
        session.commit()
        raise HTTPException(401, "Invalid email or password")
    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    session.commit()
    return user


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str
    role: str


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    if credentials is None:
        raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(credentials.credentials, _secret_key(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    user = CurrentUser(id=payload["sub"], email=payload["email"], role=payload["role"])
    request.state.current_user = user
    return user


def require_role(*allowed: str):
    def _dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed:
            raise HTTPException(403, "This role cannot perform this action")
        return user

    return _dependency


require_write_access = require_role(ROLE_ADMIN, ROLE_CLINICIAN)
require_admin = require_role(ROLE_ADMIN)

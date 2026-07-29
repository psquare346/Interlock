"""Vendor accounts — the network model's login.

A vendor registers ONCE on the platform (with a single-use invite code a
customer admin generated) and that one login serves every customer connected
to their vendor org. Same password/token discipline as customer auth in
auth.py: PBKDF2, hashed tokens, lockout, generic error messages.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import VendorOrg, VendorToken, VendorUser
from .auth import (
    LOCKOUT, MAX_FAILED_LOGINS, TOKEN_TTL, AuthError,
    _hash_password, _hash_token, _validate_password,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_invite_code() -> str:
    return secrets.token_urlsafe(24)


def hash_invite(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def register_vendor_user(
    db: Session, invite_code: str, email: str, display_name: str, password: str,
) -> VendorUser:
    email = email.strip().lower()
    _validate_password(password)

    org = db.scalars(select(VendorOrg).where(
        VendorOrg.invite_code_hash == hash_invite(invite_code), VendorOrg.active
    )).first()
    if org is None:
        raise AuthError("Invite code not recognized — ask your customer for a new one")

    if db.scalars(select(VendorUser).where(VendorUser.email == email)).first():
        raise AuthError(f"A vendor account for {email} already exists")

    salt = secrets.token_hex(16)
    user = VendorUser(
        vendor_org_id=org.id,
        email=email,
        display_name=display_name.strip() or email.split("@")[0],
        password_salt=salt,
        password_hash=_hash_password(password, salt),
    )
    org.invite_code_hash = None  # single use
    db.add(user)
    db.commit()
    return user


def login_vendor(db: Session, email: str, password: str) -> tuple[str, VendorUser]:
    generic = "Wrong email or password"
    user = db.scalars(
        select(VendorUser).where(VendorUser.email == email.strip().lower())
    ).first()
    if user is None:
        _hash_password(password, "00" * 16)
        raise AuthError(generic)

    if user.locked_until is not None:
        locked = user.locked_until
        if locked.tzinfo is None:
            locked = locked.replace(tzinfo=timezone.utc)
        if locked > _now():
            raise AuthError("Account temporarily locked after failed logins — try again later")

    if not hmac.compare_digest(
        _hash_password(password, user.password_salt), user.password_hash
    ):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = _now() + LOCKOUT
            user.failed_logins = 0
        db.commit()
        raise AuthError(generic)

    if not user.active:
        raise AuthError("Account is deactivated")

    user.failed_logins = 0
    user.locked_until = None
    token = secrets.token_hex(32)
    db.add(VendorToken(
        token_hash=_hash_token(token),
        vendor_user_id=user.id,
        expires_at=_now() + TOKEN_TTL,
    ))
    db.execute(delete(VendorToken).where(
        VendorToken.vendor_user_id == user.id, VendorToken.expires_at < _now()
    ))
    db.commit()
    return token, user


def logout_vendor(db: Session, token: str) -> None:
    db.execute(delete(VendorToken).where(VendorToken.token_hash == _hash_token(token)))
    db.commit()


def get_current_vendor(
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
) -> VendorUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Log in first")
    token = authorization.split(" ", 1)[1].strip()
    row = db.get(VendorToken, _hash_token(token))
    if row is None:
        raise HTTPException(401, "Session expired or invalid — log in again")
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < _now():
        db.delete(row)
        db.commit()
        raise HTTPException(401, "Session expired — log in again")
    user = db.get(VendorUser, row.vendor_user_id)
    if user is None or not user.active:
        raise HTTPException(401, "Account no longer active")
    return user

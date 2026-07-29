"""Accounts, sessions, and privilege checks — built to be trustworthy:

- Passwords: PBKDF2-HMAC-SHA256, 600,000 iterations, per-user random salt,
  constant-time comparison. (Stdlib-only; argon2 is a fine upgrade later.)
- Tokens: 256-bit random, 12-hour expiry. Only the SHA-256 of the token is
  stored, so a database leak cannot be replayed as live sessions.
- Brute force: 5 failed logins lock the account for 15 minutes. Login errors
  never reveal whether the email or the password was wrong.
- Identity: the first account registered in a tenant becomes its admin;
  everyone else is a member holding exactly the privileges an admin granted.
- Tenancy: endpoints derive tenant_id from the authenticated user, never
  from the request.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PRIVILEGES, AuthToken, Tenant, User, UserRole

PBKDF2_ITERATIONS = 600_000
TOKEN_TTL = timedelta(hours=12)
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
MIN_PASSWORD_LEN = 8

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LEN} characters")


# --------------------------------------------------------------------------
# Register / login / logout
# --------------------------------------------------------------------------

def register(db: Session, tenant_id: str, email: str, display_name: str,
             password: str) -> User:
    email = email.strip().lower()
    if not _EMAIL.match(email):
        raise AuthError("That does not look like an email address")
    _validate_password(password)

    # Registering into a fresh tenant bootstraps it.
    if db.get(Tenant, tenant_id) is None:
        db.add(Tenant(id=tenant_id, name=tenant_id))
        db.flush()

    existing = db.scalars(
        select(User).where(User.tenant_id == tenant_id, User.email == email)
    ).first()
    if existing:
        raise AuthError(f"An account for {email} already exists in this tenant")

    first_in_tenant = db.scalars(
        select(User).where(User.tenant_id == tenant_id)
    ).first() is None

    salt = secrets.token_hex(16)
    user = User(
        tenant_id=tenant_id,
        email=email,
        display_name=display_name.strip() or email.split("@")[0],
        password_salt=salt,
        password_hash=_hash_password(password, salt),
        role=UserRole.ADMIN if first_in_tenant else UserRole.MEMBER,
        privileges=list(PRIVILEGES) if first_in_tenant else [],
    )
    db.add(user)
    db.commit()
    return user


def login(db: Session, tenant_id: str, email: str, password: str) -> tuple[str, User]:
    generic = "Wrong email or password"  # never say which
    user = db.scalars(
        select(User).where(
            User.tenant_id == tenant_id, User.email == email.strip().lower()
        )
    ).first()
    if user is None:
        # Burn comparable time so absent accounts aren't detectable by timing.
        _hash_password(password, "00" * 16)
        raise AuthError(generic)

    if user.locked_until is not None:
        locked_until = user.locked_until
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > _now():
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
        raise AuthError("Account is deactivated — ask your admin")

    user.failed_logins = 0
    user.locked_until = None

    token = secrets.token_hex(32)
    db.add(AuthToken(
        token_hash=_hash_token(token),
        user_id=user.id,
        expires_at=_now() + TOKEN_TTL,
    ))
    # Housekeeping: drop this user's expired tokens.
    db.execute(delete(AuthToken).where(
        AuthToken.user_id == user.id, AuthToken.expires_at < _now()
    ))
    db.commit()
    return token, user


def logout(db: Session, token: str) -> None:
    db.execute(delete(AuthToken).where(AuthToken.token_hash == _hash_token(token)))
    db.commit()


# --------------------------------------------------------------------------
# Request dependencies
# --------------------------------------------------------------------------

def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Log in first")
    return authorization.split(" ", 1)[1].strip()


def get_current_user(
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
) -> User:
    row = db.get(AuthToken, _hash_token(_bearer(authorization)))
    if row is None:
        raise HTTPException(401, "Session expired or invalid — log in again")
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < _now():
        db.delete(row)
        db.commit()
        raise HTTPException(401, "Session expired — log in again")
    user = db.get(User, row.user_id)
    if user is None or not user.active:
        raise HTTPException(401, "Account no longer active")
    return user


def require(privilege: str):
    """Dependency factory: `Depends(require("catalog_publish"))`."""
    if privilege not in PRIVILEGES:
        raise ValueError(f"Unknown privilege {privilege!r}")

    def dependency(user: User = Depends(get_current_user)) -> User:
        if not user.has_privilege(privilege):
            raise HTTPException(
                403,
                f"You need the {privilege!r} privilege. "
                "An admin can grant it in the Users panel.",
            )
        return user

    return dependency


def user_out(user: User) -> dict:
    return {
        "id": user.id,
        "tenant_id": user.tenant_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role.value,
        "privileges": user.privileges or [],
        "active": user.active,
    }

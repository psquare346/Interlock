"""Accounts, sessions, and privilege checks — built to be trustworthy:

- Passwords: PBKDF2-HMAC-SHA256, 600,000 iterations, per-user random salt,
  constant-time comparison. (Stdlib-only; argon2 is a fine upgrade later.)
- Tokens: 256-bit random, 12-hour expiry. Only the SHA-256 of the token is
  stored, so a database leak cannot be replayed as live sessions.
- Brute force: 5 failed logins lock the account for 15 minutes. Login errors
  never reveal whether the email or the password was wrong.
- Identity: registration is INVITE-ONLY. An operator (tenant provisioning)
  or a tenant admin issues a single-use, email-bound invite that carries the
  tenant, role, and privileges; the invitee supplies only a display name and
  password. There is no open signup and no register-bootstraps-a-tenant path.
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
from ..models import PRIVILEGES, AuthToken, StaffInvite, Tenant, User, UserRole

PBKDF2_ITERATIONS = 600_000
TOKEN_TTL = timedelta(hours=12)
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
MIN_PASSWORD_LEN = 8
INVITE_TTL = timedelta(days=14)

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
# Invites / register / login / logout
# --------------------------------------------------------------------------

def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def create_staff_invite(
    db: Session,
    tenant_id: str,
    email: str,
    role: UserRole = UserRole.MEMBER,
    privileges: list[str] | None = None,
    invited_by: str | None = None,
) -> tuple[str, StaffInvite]:
    """Issue a single-use invite. Returns (plaintext_token, invite) — only
    the token's hash is stored, so this is the one chance to hand it over.
    Re-inviting the same email replaces any pending invite for it."""
    email = email.strip().lower()
    if not _EMAIL.match(email):
        raise AuthError("That does not look like an email address")
    if db.get(Tenant, tenant_id) is None:
        raise AuthError(f"Tenant {tenant_id!r} does not exist")

    unknown = set(privileges or []) - set(PRIVILEGES)
    if unknown:
        raise AuthError(f"Unknown privileges: {sorted(unknown)}")

    existing = db.scalars(
        select(User).where(User.tenant_id == tenant_id, User.email == email)
    ).first()
    if existing:
        raise AuthError(f"An account for {email} already exists in this tenant")

    # One pending invite per (tenant, email): a fresh invite supersedes.
    for old in db.scalars(
        select(StaffInvite).where(
            StaffInvite.tenant_id == tenant_id,
            StaffInvite.email == email,
            StaffInvite.used_at.is_(None),
        )
    ).all():
        db.delete(old)

    token = secrets.token_urlsafe(24)
    invite = StaffInvite(
        tenant_id=tenant_id,
        email=email,
        role=role,
        privileges=list(privileges or []),
        token_hash=_hash_token(token),
        invited_by=invited_by,
        expires_at=_now() + INVITE_TTL,
    )
    db.add(invite)
    db.commit()
    return token, invite


def find_invite(db: Session, token: str) -> StaffInvite | None:
    """Look up a live (unused, unexpired) invite by its plaintext token."""
    invite = db.scalars(
        select(StaffInvite).where(StaffInvite.token_hash == _hash_token(token))
    ).first()
    if invite is None or invite.used_at is not None:
        return None
    if _as_utc(invite.expires_at) < _now():
        return None
    return invite


def register(db: Session, invite_token: str, display_name: str, password: str) -> User:
    """Create an account from a single-use invite. The invite decides the
    tenant, email, role, and privileges — the invitee brings only a display
    name and password. Invalid, used, and expired tokens all get the same
    error so the endpoint isn't an oracle for live invite tokens."""
    _validate_password(password)

    invite = find_invite(db, invite_token)
    if invite is None:
        raise AuthError("Invite not recognized — it may have been used or expired. "
                        "Ask your admin for a new one.")

    existing = db.scalars(
        select(User).where(
            User.tenant_id == invite.tenant_id, User.email == invite.email
        )
    ).first()
    if existing:
        raise AuthError(f"An account for {invite.email} already exists in this tenant")

    salt = secrets.token_hex(16)
    user = User(
        tenant_id=invite.tenant_id,
        email=invite.email,
        display_name=display_name.strip() or invite.email.split("@")[0],
        password_salt=salt,
        password_hash=_hash_password(password, salt),
        role=invite.role,
        # Admin implies everything; store the full list for consistency with
        # how the UI displays admin accounts.
        privileges=list(PRIVILEGES) if invite.role is UserRole.ADMIN
        else list(invite.privileges or []),
    )
    invite.used_at = _now()
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


def staff_user_from_header(db: Session, authorization: str | None) -> User | None:
    """Non-raising variant of get_current_user: resolve an optional Bearer
    header to an active staff user, or None. For endpoints that are normally
    token-free (punchout, storefront) but grant extra paths to logged-in
    staff of the same tenant."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    row = db.get(AuthToken, _hash_token(authorization.split(" ", 1)[1].strip()))
    if row is None:
        return None
    if _as_utc(row.expires_at) < _now():
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.active:
        return None
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

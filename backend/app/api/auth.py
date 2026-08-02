"""Accounts: register (invite-only), login, logout, me — plus the admin's
user panel and staff-invite management.

Email format and password strength are validated in services/auth.py, so the
rules live in one place whichever route creates the account. There is no open
signup: every account starts from a single-use invite issued by an operator
(tenant provisioning) or a tenant admin (POST /invites).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PRIVILEGES, StaffInvite, Tenant, User, UserRole
from ..services import auth
from ..services.auth import get_current_user, require, user_out

router = APIRouter()


class RegisterIn(BaseModel):
    invite_token: str
    display_name: str = ""
    password: str


class LoginIn(BaseModel):
    tenant_id: str = "demo"
    email: str
    password: str


@router.post("/register", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    try:
        user = auth.register(db, body.invite_token, body.display_name, body.password)
        token, user = auth.login(db, user.tenant_id, user.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(422, str(e))
    return {
        "token": token,
        "user": user_out(user),
        "note": f"Welcome to {user.tenant_id} — you are an admin"
        if user.role is UserRole.ADMIN else None,
    }


@router.get("/invites/preview")
def preview_invite(token: str = Query(...), db: Session = Depends(get_db)):
    """Let the register form show who an invite is for before the invitee
    commits a password. Token-gated: the token itself is the secret."""
    invite = auth.find_invite(db, token)
    if invite is None:
        raise HTTPException(404, "Invite not recognized — it may have been used or expired")
    tenant = db.get(Tenant, invite.tenant_id)
    return {
        "tenant_id": invite.tenant_id,
        "tenant_name": tenant.name if tenant else invite.tenant_id,
        "email": invite.email,
        "role": invite.role.value,
        "expires_at": str(invite.expires_at),
    }


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    try:
        token, user = auth.login(db, body.tenant_id, body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(401, str(e))
    return {"token": token, "user": user_out(user)}


@router.post("/logout")
def logout(authorization: str | None = Header(None), db: Session = Depends(get_db)):
    if authorization and authorization.lower().startswith("bearer "):
        auth.logout(db, authorization.split(" ", 1)[1].strip())
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return user_out(user)


@router.get("/privileges")
def list_privileges():
    return PRIVILEGES


# --------------------------------------------------------------------------
# Staff invites — how every non-provisioned account gets created
# --------------------------------------------------------------------------

class InviteIn(BaseModel):
    email: str
    role: UserRole = UserRole.MEMBER
    privileges: list[str] = []


def _invite_out(i: StaffInvite) -> dict:
    return {
        "id": i.id,
        "email": i.email,
        "role": i.role.value,
        "privileges": i.privileges or [],
        "invited_by": i.invited_by,
        "expires_at": str(i.expires_at),
        "created_at": str(i.created_at),
    }


def _guard_grant(grantor: User, role: UserRole, privileges: list[str]) -> None:
    """No privilege escalation via delegation: a non-admin manage_users holder
    can neither mint admins nor grant a privilege it does not itself hold.
    Only a full admin can create admins or grant the whole vocabulary."""
    if grantor.role is UserRole.ADMIN:
        return
    if role is UserRole.ADMIN:
        raise HTTPException(403, "Only an admin can grant the admin role")
    over = set(privileges or []) - set(grantor.privileges or [])
    if over:
        raise HTTPException(
            403, f"You can only grant privileges you hold yourself; not: {sorted(over)}"
        )


@router.post("/invites", status_code=201)
def create_invite(
    body: InviteIn,
    admin: User = Depends(require("manage_users")),
    db: Session = Depends(get_db),
):
    """Issue a single-use invite for this tenant. The token is returned
    exactly once — hand the register link to the invitee out of band."""
    _guard_grant(admin, body.role, body.privileges)
    try:
        token, invite = auth.create_staff_invite(
            db, admin.tenant_id, body.email,
            role=body.role, privileges=body.privileges, invited_by=admin.email,
        )
    except auth.AuthError as e:
        raise HTTPException(422, str(e))
    return {
        **_invite_out(invite),
        "invite_token": token,
        "register_path": f"/admin#invite={token}",
        "note": "Shown once — send the link to the invitee; it dies on first use.",
    }


@router.get("/invites")
def list_invites(
    admin: User = Depends(require("manage_users")),
    db: Session = Depends(get_db),
):
    """Pending (unused) invites for this tenant, expired ones flagged."""
    rows = db.scalars(
        select(StaffInvite).where(
            StaffInvite.tenant_id == admin.tenant_id,
            StaffInvite.used_at.is_(None),
        ).order_by(StaffInvite.created_at.desc())
    ).all()
    now = auth._now()
    return [
        {**_invite_out(i), "expired": auth._as_utc(i.expires_at) < now}
        for i in rows
    ]


@router.delete("/invites/{invite_id}")
def revoke_invite(
    invite_id: str,
    admin: User = Depends(require("manage_users")),
    db: Session = Depends(get_db),
):
    invite = db.get(StaffInvite, invite_id)
    if invite is None or invite.tenant_id != admin.tenant_id or invite.used_at is not None:
        raise HTTPException(404, "Invite not found")
    db.delete(invite)
    db.commit()
    return {"revoked": invite_id}


@router.get("/users")
def list_users(
    admin: User = Depends(require("manage_users")),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(User).where(User.tenant_id == admin.tenant_id).order_by(User.created_at)
    ).all()
    return [user_out(u) for u in rows]


class UserEdit(BaseModel):
    privileges: list[str] | None = None
    role: UserRole | None = None
    active: bool | None = None


@router.patch("/users/{user_id}")
def edit_user(
    user_id: str,
    body: UserEdit,
    admin: User = Depends(require("manage_users")),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None or user.tenant_id != admin.tenant_id:
        raise HTTPException(404, "User not found")

    if body.privileges is not None:
        unknown = set(body.privileges) - set(PRIVILEGES)
        if unknown:
            raise HTTPException(422, f"Unknown privileges: {sorted(unknown)}")
    # Same anti-escalation rule as invites, plus: a non-admin cannot edit its
    # own record's role/privileges at all (no self-promotion).
    if admin.role is not UserRole.ADMIN and user.id == admin.id and (
        body.role is not None or body.privileges is not None
    ):
        raise HTTPException(403, "You cannot change your own role or privileges")
    _guard_grant(admin, body.role or user.role, body.privileges or [])

    if body.privileges is not None:
        user.privileges = body.privileges
    if body.role is not None:
        user.role = body.role
    if body.active is not None:
        user.active = body.active

    # An admin can't lock the tenant out: keep at least one active admin.
    admins_left = db.scalars(
        select(User).where(
            User.tenant_id == admin.tenant_id,
            User.role == UserRole.ADMIN,
            User.active.is_(True),
            User.id != user.id,
        )
    ).first()
    if admins_left is None and (user.role is not UserRole.ADMIN or not user.active):
        raise HTTPException(422, "That would leave the tenant with no active admin")

    db.commit()
    return user_out(user)

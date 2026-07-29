"""Accounts: register, login, logout, me — plus the admin's user panel.

Email format and password strength are validated in services/auth.py, so the
rules live in one place whichever route creates the account.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PRIVILEGES, User, UserRole
from ..services import auth
from ..services.auth import get_current_user, require, user_out

router = APIRouter()


class RegisterIn(BaseModel):
    tenant_id: str = Field("demo", pattern=r"^[a-z0-9][a-z0-9\-]{1,39}$")
    email: str
    display_name: str = ""
    password: str


class LoginIn(BaseModel):
    tenant_id: str = "demo"
    email: str
    password: str


@router.post("/register", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    try:
        auth.register(db, body.tenant_id, body.email, body.display_name, body.password)
        token, user = auth.login(db, body.tenant_id, body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(422, str(e))
    return {
        "token": token,
        "user": user_out(user),
        "note": "First account in this tenant — you are its admin"
        if user.role is UserRole.ADMIN else None,
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

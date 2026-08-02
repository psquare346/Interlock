"""Operator provisioning — how a new customer gets created.

These endpoints are for US (the platform operator), not for tenants: they are
gated by the X-Operator-Key header matching settings.OPERATOR_KEY, and they
are disabled entirely (503) when no key is configured. Tenants are created
here and ONLY here — registration never bootstraps a tenant.

POST /api/ops/tenants does the whole phase-2 onboarding in one call:
  tenant row + punchout secret + PO key + admin invite. Every credential's
  plaintext is returned exactly once; only hashes are stored.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets as _secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Tenant, User, UserRole
from ..services import auth

router = APIRouter()

_SLUG = re.compile(r"^[a-z0-9][a-z0-9\-]{1,39}$")

# Tenant ids appear in URLs next to real routes — keep the namespace clean,
# and keep obvious bait names out of customer hands.
RESERVED_TENANT_IDS = {
    "admin", "api", "app", "auth", "docs", "health", "internal", "interlock",
    "operator", "ops", "orders", "po", "punchout", "sap", "shop", "static",
    "system", "test", "vendor", "www",
}


def require_operator(x_operator_key: str | None = Header(None)) -> None:
    configured = get_settings().OPERATOR_KEY
    if not configured:
        raise HTTPException(
            503, "Operator provisioning is disabled — set OPERATOR_KEY on the server"
        )
    supplied = x_operator_key or ""
    # Compare digests so length isn't leaked either.
    if not hmac.compare_digest(
        hashlib.sha256(supplied.encode()).digest(),
        hashlib.sha256(configured.encode()).digest(),
    ):
        raise HTTPException(401, "Bad operator key")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class TenantIn(BaseModel):
    tenant_id: str
    name: str
    admin_email: str


@router.post("/tenants", status_code=201)
def provision_tenant(
    body: TenantIn,
    _: None = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """Create a customer: tenant + punchout secret + PO key + admin invite."""
    tenant_id = body.tenant_id.strip().lower()
    if not _SLUG.match(tenant_id):
        raise HTTPException(
            422, "tenant_id must be 2-40 chars: lowercase letters, digits, dashes"
        )
    if tenant_id in RESERVED_TENANT_IDS:
        raise HTTPException(422, f"{tenant_id!r} is reserved — pick another id")
    if db.get(Tenant, tenant_id) is not None:
        raise HTTPException(409, f"Tenant {tenant_id!r} already exists")

    punchout_secret = _secrets.token_urlsafe(24)
    po_key = _secrets.token_urlsafe(32)
    tenant = Tenant(
        id=tenant_id,
        name=body.name.strip() or tenant_id,
        punchout_secret_hash=_sha256(punchout_secret),
        po_key_hash=_sha256(po_key),
    )
    db.add(tenant)
    db.flush()

    try:
        invite_token, invite = auth.create_staff_invite(
            db, tenant_id, body.admin_email,
            role=UserRole.ADMIN, invited_by="operator",
        )
    except auth.AuthError as e:
        db.rollback()
        raise HTTPException(422, str(e))

    return {
        "tenant_id": tenant.id,
        "name": tenant.name,
        "admin_invite": {
            "email": invite.email,
            "invite_token": invite_token,
            "register_path": f"/admin#invite={invite_token}",
            "expires_at": str(invite.expires_at),
        },
        "punchout_secret": punchout_secret,
        "po_key": po_key,
        "sap_wiring": {
            "punchout_start": f"/api/v1/punchout/oci/start?tenant_id={tenant.id}",
            "punchout_password_param": "PASSWORD (fixed parameter in the SPRO call structure)",
            "po_receive": f"/api/v1/po/receive?tenant_id={tenant.id} (header X-PO-Key)",
        },
        "note": "Every credential above is shown exactly once — store them now. "
        "All are rotatable later (ops or tenant admin).",
    }


@router.get("/tenants")
def list_tenants(
    _: None = Depends(require_operator),
    db: Session = Depends(get_db),
):
    rows = db.scalars(select(Tenant).order_by(Tenant.created_at)).all()
    user_counts = dict(
        db.execute(
            select(User.tenant_id, func.count(User.id)).group_by(User.tenant_id)
        ).all()
    )
    return [
        {
            "tenant_id": t.id,
            "name": t.name,
            "users": user_counts.get(t.id, 0),
            "punchout_secret_configured": t.punchout_secret_hash is not None,
            "po_key_configured": t.po_key_hash is not None,
            "created_at": str(t.created_at),
        }
        for t in rows
    ]


class OpsInviteIn(BaseModel):
    email: str
    role: UserRole = UserRole.ADMIN
    privileges: list[str] = []


@router.post("/tenants/{tenant_id}/invites", status_code=201)
def ops_invite(
    tenant_id: str,
    body: OpsInviteIn,
    _: None = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """Fresh invite for an existing tenant — the 'first admin lost the link'
    path, or adding an extra admin on the customer's behalf."""
    if db.get(Tenant, tenant_id) is None:
        raise HTTPException(404, f"Tenant {tenant_id!r} not found")
    try:
        token, invite = auth.create_staff_invite(
            db, tenant_id, body.email,
            role=body.role, privileges=body.privileges, invited_by="operator",
        )
    except auth.AuthError as e:
        raise HTTPException(422, str(e))
    return {
        "email": invite.email,
        "role": invite.role.value,
        "invite_token": token,
        "register_path": f"/admin#invite={token}",
        "expires_at": str(invite.expires_at),
    }


@router.post("/tenants/{tenant_id}/punchout-secret")
def ops_rotate_punchout_secret(
    tenant_id: str,
    _: None = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """Issue or rotate a tenant's punchout secret (also available to the
    tenant's own admin via /api/tenants/punchout-secret)."""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, f"Tenant {tenant_id!r} not found")
    secret = _secrets.token_urlsafe(24)
    tenant.punchout_secret_hash = _sha256(secret)
    db.commit()
    return {
        "tenant_id": tenant.id,
        "punchout_secret": secret,
        "note": "Shown once. Update the PASSWORD parameter in the tenant's "
        "SPRO catalog call structure — old sessions keep working, new "
        "/oci/start calls need the new secret.",
    }

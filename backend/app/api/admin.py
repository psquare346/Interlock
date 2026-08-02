"""Tenants, suppliers, contracts.

Every route here derives tenant_id from the authenticated user — the request
is never trusted to say which tenant it belongs to. Master-data writes need
the manage_master_data privilege; reads need any logged-in account.
Tenant creation happens through registration (first account bootstraps the
tenant), so there is no open create-tenant endpoint.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Contract, Supplier, SupplierProtocol, Tenant, User, VendorOrg
from ..services.auth import get_current_user, require
from ..services.secrets import encrypt
from ..services.vendor_auth import hash_invite, new_invite_code

router = APIRouter()


@router.get("/tenants")
def my_tenant(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None:
        return []
    return [{"id": tenant.id, "name": tenant.name,
             "po_key_configured": tenant.po_key_hash is not None,
             "punchout_secret_configured": tenant.punchout_secret_hash is not None}]


@router.post("/tenants/po-key")
def rotate_po_key(
    user: User = Depends(require("manage_master_data")),
    db: Session = Depends(get_db),
):
    """Generate (or rotate) the integration key SAP sends on /api/v1/po/receive.
    The plaintext is returned exactly once — only its hash is stored."""
    import hashlib
    import secrets as _secrets

    key = _secrets.token_urlsafe(32)
    tenant = db.get(Tenant, user.tenant_id)
    tenant.po_key_hash = hashlib.sha256(key.encode()).hexdigest()
    db.commit()
    return {
        "po_key": key,
        "endpoint": "/api/v1/po/receive?tenant_id=" + tenant.id,
        "note": "Store this now — it is not retrievable later, only rotatable.",
    }


@router.post("/tenants/punchout-secret")
def rotate_punchout_secret(
    user: User = Depends(require("manage_master_data")),
    db: Session = Depends(get_db),
):
    """Generate (or rotate) the punchout front-door secret SAP sends as the
    PASSWORD parameter on /oci/start. Same discipline as the PO key: the
    plaintext is returned exactly once, only its hash is stored. Until a
    secret exists, punchout for this tenant is closed."""
    import hashlib
    import secrets as _secrets

    secret = _secrets.token_urlsafe(24)
    tenant = db.get(Tenant, user.tenant_id)
    tenant.punchout_secret_hash = hashlib.sha256(secret.encode()).hexdigest()
    db.commit()
    return {
        "punchout_secret": secret,
        "endpoint": f"/api/v1/punchout/oci/start?tenant_id={tenant.id}",
        "sap_note": "Basis: add PASSWORD as a fixed parameter with this value "
        "in the SPRO web-service call structure. Requisitioners never see it.",
        "note": "Store this now — it is not retrievable later, only rotatable. "
        "Rotating invalidates the previous secret immediately.",
    }


# --------------------------------------------------------------------------
# Suppliers
# --------------------------------------------------------------------------

class SupplierIn(BaseModel):
    code: str
    name: str
    sap_vendor_no: str | None = None
    protocol: SupplierProtocol = SupplierProtocol.HOSTED
    punchout_url: str | None = None
    from_domain: str | None = None
    from_identity: str | None = None
    to_domain: str | None = None
    to_identity: str | None = None
    # Accepted here, stored encrypted, never echoed back.
    shared_secret: str | None = None
    deployment_mode: str = "test"


def _supplier_out(s: Supplier) -> dict:
    return {
        "id": s.id,
        "tenant_id": s.tenant_id,
        "code": s.code,
        "name": s.name,
        "sap_vendor_no": s.sap_vendor_no,
        "protocol": s.protocol.value,
        "punchout_url": s.punchout_url,
        "deployment_mode": s.deployment_mode,
        "active": s.active,
        "has_shared_secret": s.shared_secret_encrypted is not None,
        "has_confirmed_mapping": s.confirmed_mapping is not None,
    }


@router.post("/suppliers", status_code=201)
def create_supplier(
    body: SupplierIn,
    user: User = Depends(require("manage_master_data")),
    db: Session = Depends(get_db),
):
    existing = db.scalars(
        select(Supplier).where(
            Supplier.tenant_id == user.tenant_id, Supplier.code == body.code
        )
    ).first()
    if existing:
        raise HTTPException(409, f"Supplier {body.code!r} already exists")

    if body.protocol is SupplierProtocol.CXML and not body.punchout_url:
        raise HTTPException(422, "cXML suppliers need a punchout_url")

    supplier = Supplier(
        **body.model_dump(exclude={"shared_secret"}),
        tenant_id=user.tenant_id,
        shared_secret_encrypted=encrypt(body.shared_secret) if body.shared_secret else None,
    )
    db.add(supplier)
    db.commit()
    return _supplier_out(supplier)


@router.get("/suppliers")
def list_suppliers(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Supplier).where(Supplier.tenant_id == user.tenant_id).order_by(Supplier.code)
    ).all()
    return [_supplier_out(s) for s in rows]


# --------------------------------------------------------------------------
# Vendor orgs — connect a supplier record to a global vendor identity
# --------------------------------------------------------------------------

class VendorOrgIn(BaseModel):
    name: str | None = None
    contact_email: str | None = None


@router.post("/suppliers/{supplier_id}/vendor-org", status_code=201)
def invite_vendor(
    supplier_id: str,
    body: VendorOrgIn,
    user: User = Depends(require("manage_master_data")),
    db: Session = Depends(get_db),
):
    """Create (or re-invite) the vendor org behind a supplier and return a
    single-use invite code the vendor uses to register their platform login.
    One vendor org can serve many customers — if this supplier is already
    linked, this just issues a fresh invite code."""
    supplier = db.get(Supplier, supplier_id)
    if supplier is None or supplier.tenant_id != user.tenant_id:
        raise HTTPException(404, "Supplier not found")

    org = db.get(VendorOrg, supplier.vendor_org_id) if supplier.vendor_org_id else None
    if org is None:
        org = VendorOrg(
            name=body.name or supplier.name,
            contact_email=(body.contact_email or None),
        )
        db.add(org)
        db.flush()
        supplier.vendor_org_id = org.id

    code = new_invite_code()
    org.invite_code_hash = hash_invite(code)
    db.commit()
    return {
        "vendor_org_id": org.id,
        "vendor_org_name": org.name,
        "invite_code": code,
        "register_at": "/vendor",
        "note": "Single use — hand it to the vendor; it dies on first registration.",
    }


# --------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------

class ContractIn(BaseModel):
    supplier_code: str | None = None
    contract_no: str
    sap_agreement_no: str | None = None
    valid_from: date
    valid_to: date
    currency: str = "USD"
    precedence: int = 0


@router.post("/contracts", status_code=201)
def create_contract(
    body: ContractIn,
    user: User = Depends(require("manage_master_data")),
    db: Session = Depends(get_db),
):
    if body.valid_from > body.valid_to:
        raise HTTPException(422, "valid_from is after valid_to")

    supplier_id = None
    if body.supplier_code:
        supplier = db.scalars(
            select(Supplier).where(
                Supplier.tenant_id == user.tenant_id,
                Supplier.code == body.supplier_code,
            )
        ).first()
        if supplier is None:
            raise HTTPException(404, f"Supplier {body.supplier_code!r} not found")
        supplier_id = supplier.id

    existing = db.scalars(
        select(Contract).where(
            Contract.tenant_id == user.tenant_id,
            Contract.contract_no == body.contract_no,
        )
    ).first()
    if existing:
        raise HTTPException(409, f"Contract {body.contract_no!r} already exists")

    contract = Contract(
        tenant_id=user.tenant_id,
        supplier_id=supplier_id,
        contract_no=body.contract_no,
        sap_agreement_no=body.sap_agreement_no,
        valid_from=body.valid_from,
        valid_to=body.valid_to,
        currency=body.currency.upper(),
        precedence=body.precedence,
    )
    db.add(contract)
    db.commit()
    return {
        "id": contract.id,
        "contract_no": contract.contract_no,
        "supplier_code": body.supplier_code,
        "valid_from": str(contract.valid_from),
        "valid_to": str(contract.valid_to),
        "currency": contract.currency,
    }


@router.get("/contracts")
def list_contracts(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Contract).where(Contract.tenant_id == user.tenant_id).order_by(Contract.contract_no)
    ).all()
    return [
        {
            "id": c.id,
            "contract_no": c.contract_no,
            "valid_from": str(c.valid_from),
            "valid_to": str(c.valid_to),
            "currency": c.currency,
            "active": c.active,
        }
        for c in rows
    ]

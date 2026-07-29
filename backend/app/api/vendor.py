"""Vendor portal API — the network model in action.

One vendor login serves every customer: the order queue below joins across
all tenants whose supplier records link to the vendor's org. Vendors see and
touch ONLY orders addressed to them; customer master data stays invisible.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Order, OrderEvent, OrderLine, OrderStatus, Supplier, Tenant, VendorOrg, VendorUser,
)
from ..services.auth import AuthError
from ..services.orders import PoError, transition
from ..services.vendor_auth import (
    get_current_vendor, login_vendor, logout_vendor, register_vendor_user,
)

router = APIRouter()


# --------------------------------------------------------------------------
# Account
# --------------------------------------------------------------------------

class VendorRegister(BaseModel):
    invite_code: str
    email: str
    display_name: str = ""
    password: str


class VendorLogin(BaseModel):
    email: str
    password: str


@router.post("/register", status_code=201)
def register(body: VendorRegister, db: Session = Depends(get_db)):
    try:
        user = register_vendor_user(
            db, body.invite_code, body.email, body.display_name, body.password
        )
    except AuthError as e:
        raise HTTPException(400, str(e))
    return {"id": user.id, "email": user.email, "vendor_org_id": user.vendor_org_id}


@router.post("/login")
def login(body: VendorLogin, db: Session = Depends(get_db)):
    try:
        token, user = login_vendor(db, body.email, body.password)
    except AuthError as e:
        raise HTTPException(401, str(e))
    org = db.get(VendorOrg, user.vendor_org_id)
    return {
        "token": token,
        "user": {"email": user.email, "display_name": user.display_name,
                 "vendor_org": org.name if org else None},
    }


@router.post("/logout")
def logout(authorization: str | None = Header(None), db: Session = Depends(get_db)):
    if authorization and authorization.lower().startswith("bearer "):
        logout_vendor(db, authorization.split(" ", 1)[1].strip())
    return {"ok": True}


@router.get("/me")
def me(vendor: VendorUser = Depends(get_current_vendor), db: Session = Depends(get_db)):
    org = db.get(VendorOrg, vendor.vendor_org_id)
    customers = db.scalars(
        select(Supplier).where(Supplier.vendor_org_id == vendor.vendor_org_id)
    ).all()
    tenants = {
        t.id: t.name for t in db.scalars(
            select(Tenant).where(Tenant.id.in_({s.tenant_id for s in customers}))
        ).all()
    } if customers else {}
    return {
        "email": vendor.email,
        "display_name": vendor.display_name,
        "vendor_org": org.name if org else None,
        "customers": [
            {"tenant": tenants.get(s.tenant_id, s.tenant_id), "supplier_code": s.code}
            for s in customers
        ],
    }


# --------------------------------------------------------------------------
# Order queue
# --------------------------------------------------------------------------

def _vendor_supplier_ids(db: Session, vendor: VendorUser) -> list[str]:
    return list(db.scalars(
        select(Supplier.id).where(Supplier.vendor_org_id == vendor.vendor_org_id)
    ).all())


def _vendor_order(db: Session, vendor: VendorUser, order_id: str) -> Order:
    order = db.get(Order, order_id)
    if order is None or order.supplier_id not in set(_vendor_supplier_ids(db, vendor)):
        raise HTTPException(404, "Order not found")
    return order


@router.get("/orders")
def order_queue(
    vendor: VendorUser = Depends(get_current_vendor),
    db: Session = Depends(get_db),
    limit: int = 100,
):
    supplier_ids = _vendor_supplier_ids(db, vendor)
    if not supplier_ids:
        return []
    orders = db.scalars(
        select(Order).where(Order.supplier_id.in_(supplier_ids))
        .order_by(Order.created_at.desc()).limit(min(limit, 500))
    ).all()
    tenants = {
        t.id: t.name for t in db.scalars(
            select(Tenant).where(Tenant.id.in_({o.tenant_id for o in orders}))
        ).all()
    } if orders else {}
    return [
        {
            "id": o.id,
            "customer": tenants.get(o.tenant_id, o.tenant_id),
            "sap_po_number": o.sap_po_number,
            "status": o.status.value,
            "currency": o.currency,
            "total": float(o.total) if o.total is not None else None,
            "ordered_at": str(o.ordered_at) if o.ordered_at else None,
            "created_at": o.created_at.isoformat(),
        }
        for o in orders
    ]


@router.get("/orders/{order_id}")
def order_detail(
    order_id: str,
    vendor: VendorUser = Depends(get_current_vendor),
    db: Session = Depends(get_db),
):
    order = _vendor_order(db, vendor, order_id)
    lines = db.scalars(
        select(OrderLine).where(OrderLine.order_id == order.id).order_by(OrderLine.line_no)
    ).all()
    events = db.scalars(
        select(OrderEvent).where(OrderEvent.order_id == order.id)
        .order_by(OrderEvent.created_at)
    ).all()
    tenant = db.get(Tenant, order.tenant_id)
    return {
        "id": order.id,
        "customer": tenant.name if tenant else order.tenant_id,
        "sap_po_number": order.sap_po_number,
        "status": order.status.value,
        "currency": order.currency,
        "total": float(order.total) if order.total is not None else None,
        "ordered_at": str(order.ordered_at) if order.ordered_at else None,
        # The vendor sees quantities and agreed prices — not the buyer's
        # contract analysis (expected price / verdicts stay buyer-side).
        "lines": [
            {
                "line_no": l.line_no,
                "part": l.supplier_part_id,
                "description": l.description,
                "quantity": float(l.quantity),
                "uom": l.uom,
                "unit_price": float(l.unit_price),
                "currency": l.currency,
            }
            for l in lines
        ],
        "events": [
            {"type": e.type, "data": e.data, "created_at": e.created_at.isoformat()}
            for e in events
        ],
    }


class ShipBody(BaseModel):
    tracking_number: str
    carrier: str = ""


@router.post("/orders/{order_id}/acknowledge")
def acknowledge(
    order_id: str,
    vendor: VendorUser = Depends(get_current_vendor),
    db: Session = Depends(get_db),
):
    order = _vendor_order(db, vendor, order_id)
    try:
        transition(db, order, OrderStatus.ACKNOWLEDGED, actor=vendor.email)
    except PoError as e:
        raise HTTPException(409, str(e))
    return {"status": order.status.value}


@router.post("/orders/{order_id}/ship")
def ship(
    order_id: str,
    body: ShipBody,
    vendor: VendorUser = Depends(get_current_vendor),
    db: Session = Depends(get_db),
):
    order = _vendor_order(db, vendor, order_id)
    try:
        transition(
            db, order, OrderStatus.SHIPPED, actor=vendor.email,
            data={"tracking_number": body.tracking_number, "carrier": body.carrier},
        )
    except PoError as e:
        raise HTTPException(409, str(e))
    return {"status": order.status.value}

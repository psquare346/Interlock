"""Buyer-side order tracking: every PO that flowed through the platform,
keyed by the SAP PO number the customer's team already uses."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Order, OrderEvent, OrderLine, OrderStatus, Supplier, User
from ..services.auth import get_current_user
from ..services.orders import PoError, transition

router = APIRouter()


def _order_out(o: Order, supplier: Supplier | None) -> dict:
    return {
        "id": o.id,
        "sap_po_number": o.sap_po_number,
        "version": o.sap_po_version,
        "status": o.status.value,
        "supplier": supplier.name if supplier else None,
        "supplier_code": supplier.code if supplier else None,
        "currency": o.currency,
        "total": float(o.total) if o.total is not None else None,
        "ordered_at": str(o.ordered_at) if o.ordered_at else None,
        "created_at": o.created_at.isoformat(),
        "updated_at": o.updated_at.isoformat(),
    }


def _line_out(l: OrderLine) -> dict:
    return {
        "line_no": l.line_no,
        "part": l.supplier_part_id,
        "description": l.description,
        "quantity": float(l.quantity),
        "uom": l.uom,
        "unit_price": float(l.unit_price),
        "currency": l.currency,
        "expected_unit_price": (
            float(l.expected_unit_price) if l.expected_unit_price is not None else None
        ),
        "price_verdict": l.price_verdict.value if l.price_verdict else None,
        "price_delta": float(l.price_delta) if l.price_delta is not None else None,
    }


def _own_order(db: Session, user: User, order_id: str) -> Order:
    order = db.get(Order, order_id)
    if order is None or order.tenant_id != user.tenant_id:
        raise HTTPException(404, "Order not found")
    return order


@router.get("")
def list_orders(
    status: OrderStatus | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 100,
):
    query = select(Order).where(Order.tenant_id == user.tenant_id)
    if status is not None:
        query = query.where(Order.status == status)
    orders = db.scalars(query.order_by(Order.created_at.desc()).limit(min(limit, 500))).all()
    suppliers = {
        s.id: s for s in db.scalars(
            select(Supplier).where(Supplier.tenant_id == user.tenant_id)
        ).all()
    }
    return [_order_out(o, suppliers.get(o.supplier_id)) for o in orders]


@router.get("/{order_id}")
def get_order(
    order_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _own_order(db, user, order_id)
    supplier = db.get(Supplier, order.supplier_id) if order.supplier_id else None
    lines = db.scalars(
        select(OrderLine).where(OrderLine.order_id == order.id).order_by(OrderLine.line_no)
    ).all()
    events = db.scalars(
        select(OrderEvent).where(OrderEvent.order_id == order.id)
        .order_by(OrderEvent.created_at)
    ).all()
    out = _order_out(order, supplier)
    out["lines"] = [_line_out(l) for l in lines]
    out["events"] = [
        {
            "type": e.type,
            "actor": e.actor,
            "data": e.data,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]
    out["price_summary"] = {
        "overpaid_lines": sum(1 for l in lines if l.price_verdict and l.price_verdict.value == "overpaid"),
        "off_catalog_lines": sum(1 for l in lines if l.price_verdict and l.price_verdict.value == "off_catalog"),
        "leakage": float(sum(
            l.price_delta for l in lines
            if l.price_delta is not None and l.price_delta > 0
        ) or 0),
    }
    return out


class Note(BaseModel):
    text: str


@router.post("/{order_id}/note", status_code=201)
def add_note(
    order_id: str,
    body: Note,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _own_order(db, user, order_id)
    db.add(OrderEvent(
        tenant_id=order.tenant_id, order_id=order.id,
        type="note", actor=user.email, data={"text": body.text[:2000]},
    ))
    db.commit()
    return {"ok": True}


@router.post("/{order_id}/mark-delivered")
def mark_delivered(
    order_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Buyer confirms goods receipt (mirrors the GR they post in SAP)."""
    order = _own_order(db, user, order_id)
    try:
        transition(db, order, OrderStatus.DELIVERED, actor=user.email)
    except PoError as e:
        raise HTTPException(409, str(e))
    return {"status": order.status.value}

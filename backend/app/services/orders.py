"""PO receipt and order lifecycle.

The PO is the platform's reference key: S/4 pushes it here (SOAP/IDoc XML or
JSON), we match it back to the supplier, catalog, and contract, verify every
line against the contracted price (the product's core promise), and track it
through vendor acknowledgement → shipment → delivery.

Idempotency (SCALE.md D4): (tenant_id, sap_po_number) is unique. A re-push —
SAP retries, PO change orders — updates the existing order in place, bumps
sap_po_version, and appends an 'updated' event. It never duplicates.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    Order, OrderEvent, OrderLine, OrderStatus, PriceVerdict, Supplier,
)
from .pricing import resolve_price

# A paid price within 0.1% of the contracted price counts as a match —
# covers rounding differences between SAP pricing and ours.
MATCH_TOLERANCE = Decimal("0.001")


class PoError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Payload parsing
# --------------------------------------------------------------------------

def parse_po_json(payload: dict) -> dict:
    """Validate/normalize the JSON PO shape. Raises PoError with a reason."""
    po_number = str(payload.get("po_number") or "").strip()
    if not po_number:
        raise PoError("po_number is required")
    lines_in = payload.get("lines") or []
    if not lines_in:
        raise PoError("A PO needs at least one line")

    lines = []
    for i, l in enumerate(lines_in, start=1):
        part = str(l.get("part") or l.get("supplier_part_id") or "").strip() or None
        try:
            qty = float(l.get("quantity") or 1)
            price = float(l["unit_price"])
        except (KeyError, TypeError, ValueError):
            raise PoError(f"Line {i}: quantity and unit_price must be numbers")
        lines.append({
            "line_no": int(l.get("line_no") or i),
            "part": part,
            "description": (l.get("description") or "")[:200] or None,
            "quantity": qty,
            "uom": (l.get("uom") or None),
            "unit_price": price,
            "currency": l.get("currency"),
        })

    ordered_at = None
    if payload.get("ordered_at"):
        try:
            ordered_at = date.fromisoformat(str(payload["ordered_at"])[:10])
        except ValueError:
            raise PoError(f"ordered_at {payload['ordered_at']!r} is not a date")

    supplier = payload.get("supplier") or {}
    return {
        "po_number": po_number,
        "currency": (payload.get("currency") or "USD")[:3],
        "ordered_at": ordered_at,
        "sap_vendor_no": supplier.get("sap_vendor_no"),
        "supplier_code": supplier.get("code"),
        "lines": lines,
    }


def parse_po_idoc(xml_bytes: bytes) -> dict:
    """Parse the ORDERS05 IDoc-XML subset S/4 output management produces.

    Fields read (everything else is preserved only in the raw payload):
      E1EDK01/BELNR   PO number          E1EDK01/CURCY    currency
      E1EDK03[IDDAT=012]/DATUM           document date (YYYYMMDD)
      E1EDKA1[PARVW=LF]/PARTN            supplier's SAP vendor number
      E1EDP01/POSEX MENGE MENEE NETPR    line no, qty, unit, net price
      E1EDP19[QUALF=002]/IDTNR KTEXT     vendor part number + description
                (falls back to QUALF=001, the buyer-side material)
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise PoError(f"Not well-formed XML: {e}")

    def text(el, tag):
        node = el.find(f".//{tag}") if el is not None else None
        return node.text.strip() if node is not None and node.text else None

    header = root.find(".//E1EDK01")
    if header is None:
        raise PoError("No E1EDK01 header segment — is this an ORDERS05 IDoc?")
    po_number = text(header, "BELNR")
    if not po_number:
        raise PoError("E1EDK01/BELNR (PO number) is missing")

    ordered_at = None
    for k03 in root.iter("E1EDK03"):
        if text(k03, "IDDAT") == "012" and text(k03, "DATUM"):
            raw = text(k03, "DATUM")
            ordered_at = date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
            break

    sap_vendor_no = None
    for ka1 in root.iter("E1EDKA1"):
        if text(ka1, "PARVW") == "LF":
            sap_vendor_no = text(ka1, "PARTN")
            break

    lines = []
    for n, p01 in enumerate(root.iter("E1EDP01"), start=1):
        part = desc = None
        fallback = None
        for p19 in p01.iter("E1EDP19"):
            qualf = text(p19, "QUALF")
            if qualf == "002":
                part, desc = text(p19, "IDTNR"), text(p19, "KTEXT")
                break
            if qualf == "001" and fallback is None:
                fallback = (text(p19, "IDTNR"), text(p19, "KTEXT"))
        if part is None and fallback:
            part, desc = fallback

        try:
            qty = float(text(p01, "MENGE") or 1)
            price = float(text(p01, "NETPR") or 0)
        except ValueError:
            raise PoError(f"Line {n}: MENGE/NETPR not numeric")
        posex = text(p01, "POSEX")
        lines.append({
            "line_no": int(posex) if posex and posex.isdigit() else n,
            "part": part,
            "description": desc,
            "quantity": qty,
            "uom": text(p01, "MENEE"),
            "unit_price": price,
            "currency": text(p01, "CURCY"),
        })
    if not lines:
        raise PoError("No E1EDP01 line segments found")

    return {
        "po_number": po_number,
        "currency": text(header, "CURCY") or "USD",
        "ordered_at": ordered_at,
        "sap_vendor_no": sap_vendor_no,
        "supplier_code": None,
        "lines": lines,
    }


# --------------------------------------------------------------------------
# Receipt
# --------------------------------------------------------------------------

def _find_supplier(db: Session, tenant_id: str, parsed: dict) -> Supplier | None:
    if parsed.get("sap_vendor_no"):
        s = db.scalars(select(Supplier).where(
            Supplier.tenant_id == tenant_id,
            Supplier.sap_vendor_no == parsed["sap_vendor_no"],
        )).first()
        if s:
            return s
    if parsed.get("supplier_code"):
        return db.scalars(select(Supplier).where(
            Supplier.tenant_id == tenant_id,
            Supplier.code == parsed["supplier_code"],
        )).first()
    return None


def _verify_line(db: Session, tenant_id: str, line: dict, on: date | None) -> dict:
    """Price truth for one line: paid vs. contract-resolved price."""
    from ..api.pricing import _find_item  # shared published-item lookup

    verdict = {"item_id": None, "contract_id": None,
               "expected_unit_price": None, "price_verdict": PriceVerdict.OFF_CATALOG,
               "price_delta": None}
    if not line["part"]:
        return verdict
    item = _find_item(db, tenant_id, line["part"])
    if item is None:
        return verdict

    resolved = resolve_price(db, item, int(line["quantity"]) or 1, on=on)
    expected = resolved.effective_unit_price
    paid = Decimal(str(line["unit_price"]))
    delta_each = paid - expected

    if expected > 0 and abs(delta_each) <= expected * MATCH_TOLERANCE:
        v = PriceVerdict.MATCH
    elif delta_each > 0:
        v = PriceVerdict.OVERPAID
    else:
        v = PriceVerdict.UNDERPAID

    verdict.update({
        "item_id": item.id,
        "contract_id": resolved.contract_id,
        "expected_unit_price": float(expected),
        "price_verdict": v,
        "price_delta": float(
            (delta_each * Decimal(str(line["quantity"]))).quantize(Decimal("0.01"))
        ),
    })
    return verdict


def receive_po(
    db: Session, tenant_id: str, parsed: dict,
    *, source: str, raw_payload_key: str | None = None,
) -> tuple[Order, bool]:
    """Create or update (idempotent) the order for this PO. Returns (order, created)."""
    supplier = _find_supplier(db, tenant_id, parsed)
    on = parsed.get("ordered_at")

    order = db.scalars(select(Order).where(
        Order.tenant_id == tenant_id, Order.sap_po_number == parsed["po_number"]
    )).first()
    created = order is None

    if created:
        order = Order(tenant_id=tenant_id, sap_po_number=parsed["po_number"])
        db.add(order)
    else:
        order.sap_po_version += 1
        db.execute(delete(OrderLine).where(OrderLine.order_id == order.id))

    order.supplier_id = supplier.id if supplier else None
    order.currency = parsed["currency"]
    order.ordered_at = on
    order.source = source
    if raw_payload_key:
        order.raw_payload_key = raw_payload_key
    order.updated_at = _now()
    db.flush()

    total = Decimal("0")
    for line in parsed["lines"]:
        checked = _verify_line(db, tenant_id, line, on)
        total += Decimal(str(line["unit_price"])) * Decimal(str(line["quantity"]))
        db.add(OrderLine(
            tenant_id=tenant_id, order_id=order.id,
            line_no=line["line_no"], supplier_part_id=line["part"],
            description=line["description"], quantity=line["quantity"],
            uom=line["uom"], unit_price=line["unit_price"],
            currency=line["currency"] or parsed["currency"],
            **checked,
        ))
    order.total = float(total.quantize(Decimal("0.01")))

    db.add(OrderEvent(
        tenant_id=tenant_id, order_id=order.id,
        type="received" if created else "updated",
        actor="sap",
        data={"version": order.sap_po_version, "lines": len(parsed["lines"])},
    ))
    db.commit()
    return order, created


# --------------------------------------------------------------------------
# Lifecycle transitions (vendor portal calls these)
# --------------------------------------------------------------------------

_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.RECEIVED: {OrderStatus.ACKNOWLEDGED, OrderStatus.SHIPPED, OrderStatus.CANCELLED},
    OrderStatus.ACKNOWLEDGED: {OrderStatus.SHIPPED, OrderStatus.CANCELLED},
    OrderStatus.SHIPPED: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}


def transition(
    db: Session, order: Order, to: OrderStatus, *, actor: str, data: dict | None = None,
) -> Order:
    if to not in _TRANSITIONS[order.status]:
        raise PoError(f"Cannot go from {order.status.value} to {to.value}")
    order.status = to
    order.updated_at = _now()
    db.add(OrderEvent(
        tenant_id=order.tenant_id, order_id=order.id,
        type=to.value, actor=actor, data=data or {},
    ))
    db.commit()
    return order

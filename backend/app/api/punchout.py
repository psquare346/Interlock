"""OCI punchout: start (from SAP), cart, return (back to SAP's HOOK_URL).

The round trip:
  1. SAP calls GET/POST /oci/start with HOOK_URL — we open a session, store the
     hook encrypted, and hand the user a storefront of published items.
  2. The user builds a cart (storefront UI, or the JSON cart API below).
  3. /return renders an auto-submitting HTML form of OCI NEW_ITEM-* fields
     posting to the stored HOOK_URL. Prices were resolved by code at add-time;
     quantity changes inside SAP do NOT re-resolve — which is why the
     storefront surfaces the next quantity break.

For testing without an S/4 system, /oci/mock-hook plays SAP: point HOOK_URL
at it and it echoes back whatever the cart posted.

_encrypt/_decrypt live in services/secrets.py — Fernet when ENCRYPTION_KEY is
set, dev obfuscation otherwise. Swap for KMS before production.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import PunchoutSession, Supplier
from ..services.pricing import resolve_price
from ..services.secrets import decrypt as _decrypt, encrypt as _encrypt
from .pricing import _find_item

router = APIRouter()


@router.get("/oci/start")
@router.post("/oci/start")
async def oci_start(
    request: Request,
    tenant_id: str = Query("demo"),
    db: Session = Depends(get_db),
):
    """Entry point SAP calls. Accepts HOOK_URL as query or form field."""
    params = dict(request.query_params)
    if request.method == "POST":
        form = await request.form()
        params.update({k: str(v) for k, v in form.items()})

    hook_url = params.get("HOOK_URL") or params.get("hook_url")
    if not hook_url:
        raise HTTPException(422, "HOOK_URL is required — SAP sends it on every punchout")

    session = PunchoutSession(
        tenant_id=tenant_id,
        protocol="oci",
        oci_version=get_settings().SAP_OCI_VERSION,
        hook_url_encrypted=_encrypt(hook_url),
        sap_user=params.get("USERNAME") or params.get("username"),
        cart=[],
    )
    db.add(session)
    db.commit()

    # A real SAP punchout opens this URL in the employee's browser — hand them
    # the storefront page. API/JSON callers (tests, the admin console) get the
    # session descriptor as before.
    if "text/html" in (request.headers.get("accept") or ""):
        return RedirectResponse(
            f"/shop?session={session.id}&tenant_id={tenant_id}", status_code=302
        )

    return {
        "session_id": session.id,
        "oci_version": session.oci_version,
        "storefront": f"/api/catalog/items?tenant_id={tenant_id}",
        "cart_add": f"/api/punchout/sessions/{session.id}/cart/add",
        "return_url": f"/api/punchout/sessions/{session.id}/return",
    }


class CartAdd(BaseModel):
    part: str
    quantity: int = 1


@router.post("/sessions/{session_id}/cart/add")
def cart_add(session_id: str, body: CartAdd, db: Session = Depends(get_db)):
    session = db.get(PunchoutSession, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    if session.status != "open":
        raise HTTPException(409, f"Session is {session.status}")

    item = _find_item(db, session.tenant_id, body.part)
    if item is None:
        raise HTTPException(404, f"No published item {body.part!r}")

    price = resolve_price(db, item, body.quantity)
    supplier = db.get(Supplier, item.supplier_id)

    line = {
        "part": item.supplier_part_id,
        "description": item.description,
        "quantity": body.quantity,
        "unit": item.uom_sap or "EA",
        "unit_price": float(price.unit_price),
        "price_unit": price.price_unit,
        "currency": price.currency,
        "vendor": supplier.sap_vendor_no or "",
        "material_group": item.material_group or "",
        "lead_time_days": item.lead_time_days,
        "next_break": price.next_break,
    }
    session.cart = (session.cart or []) + [line]
    db.commit()
    return {"session_id": session.id, "lines": len(session.cart), "added": line}


@router.post("/sessions/{session_id}/return")
def oci_return(
    session_id: str,
    format: str = Query("html", pattern="^(html|json)$"),
    db: Session = Depends(get_db),
):
    """Close the session and send the cart back to SAP as OCI NEW_ITEM-* fields."""
    session = db.get(PunchoutSession, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    if session.status != "open":
        raise HTTPException(409, f"Session is {session.status}")
    if not session.cart:
        raise HTTPException(409, "Cart is empty")

    hook_url = _decrypt(session.hook_url_encrypted)
    fields: dict[str, str] = {}
    for n, line in enumerate(session.cart, start=1):
        fields[f"NEW_ITEM-DESCRIPTION[{n}]"] = line["description"] or ""
        fields[f"NEW_ITEM-QUANTITY[{n}]"] = str(line["quantity"])
        fields[f"NEW_ITEM-UNIT[{n}]"] = line["unit"]
        fields[f"NEW_ITEM-PRICE[{n}]"] = f"{line['unit_price']:.4f}"
        fields[f"NEW_ITEM-PRICEUNIT[{n}]"] = str(line["price_unit"])
        fields[f"NEW_ITEM-CURRENCY[{n}]"] = line["currency"]
        fields[f"NEW_ITEM-VENDOR[{n}]"] = line["vendor"]
        fields[f"NEW_ITEM-VENDORMAT[{n}]"] = line["part"]
        if line.get("material_group"):
            fields[f"NEW_ITEM-MATGROUP[{n}]"] = line["material_group"]
        if line.get("lead_time_days") is not None:
            fields[f"NEW_ITEM-LEADTIME[{n}]"] = str(line["lead_time_days"])

    session.status = "returned"
    session.returned_at = datetime.now(timezone.utc)
    db.commit()

    if format == "json":
        return {"hook_url": hook_url, "fields": fields}

    inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in fields.items()
    )
    page = f"""<!doctype html><html><body onload="document.forms[0].submit()">
<p>Returning {len(session.cart)} line(s) to SAP&hellip;</p>
<form method="post" action="{html.escape(hook_url)}" accept-charset="utf-8">
{inputs}
<noscript><button type="submit">Continue to SAP</button></noscript>
</form></body></html>"""
    return HTMLResponse(page)


@router.post("/oci/mock-hook")
async def mock_hook(request: Request):
    """Test double for SAP's HOOK_URL. Echoes the OCI fields it received."""
    form = await request.form()
    return {"received_fields": {k: str(v) for k, v in form.items()}}


@router.get("/sessions/{session_id}")
def get_session(session_id: str, db: Session = Depends(get_db)):
    session = db.get(PunchoutSession, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    return {
        "id": session.id,
        "status": session.status,
        "protocol": session.protocol,
        "oci_version": session.oci_version,
        "sap_user": session.sap_user,
        "cart": session.cart,
        "created_at": str(session.created_at),
        "returned_at": str(session.returned_at) if session.returned_at else None,
    }

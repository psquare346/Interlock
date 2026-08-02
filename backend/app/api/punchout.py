"""OCI punchout: start (from SAP), cart, return (back to SAP's HOOK_URL).

The round trip:
  1. SAP calls GET/POST /oci/start with HOOK_URL — we open a session, store the
     hook encrypted, and hand the user a storefront of published items.
  2. The user builds a cart (storefront UI, or the JSON cart API below).
  3. /return renders an auto-submitting HTML form of OCI NEW_ITEM-* fields
     posting to the stored HOOK_URL. Prices were resolved by code at add-time;
     quantity changes inside SAP do NOT re-resolve — which is why the
     storefront surfaces the next quantity break.

The front door is credentialed: every /oci/start call must carry the tenant's
punchout secret (a fixed PASSWORD parameter in the SPRO catalog call
structure — the requisitioner never sees it), OR a logged-in staff bearer
token of the same tenant (the admin console's demo loop). A tenant with no
secret configured is closed to punchout — there is no open fallback. This is
what makes one tenant's catalog (and negotiated prices) unreachable to
strangers and to every other tenant.

For testing without an S/4 system, /oci/mock-hook plays SAP: point HOOK_URL
at it and it echoes back whatever the cart posted.

_encrypt/_decrypt live in services/secrets.py — Fernet when ENCRYPTION_KEY is
set, dev obfuscation otherwise. Swap for KMS before production.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import PunchoutSession, Supplier, Tenant
from ..services.auth import staff_user_from_header
from ..services.pricing import resolve_price
from ..services.secrets import decrypt as _decrypt, encrypt as _encrypt
from .pricing import _find_item

router = APIRouter()

# One message for every failure mode (unknown tenant, no secret configured,
# wrong secret) so the endpoint is not an oracle for tenant ids or their state.
_FRONT_DOOR_401 = (
    "Punchout credential missing or invalid. SAP must send the tenant's "
    "punchout secret as the PASSWORD parameter (SPRO catalog call structure); "
    "a tenant admin can rotate it in the console."
)

# A punchout session is a bearer key to a tenant's catalog: cap its lifetime so
# an old session id cannot serve the catalog forever. SAP opens a fresh one on
# every punchout, so a working day of validity is generous.
PUNCHOUT_SESSION_TTL = timedelta(hours=12)

# Same length as a real SHA-256 hex digest, so the credential compare runs in
# uniform time whether or not a tenant/secret exists (no timing oracle).
_DUMMY_SECRET_HASH = "0" * 64


def session_is_live(session: PunchoutSession | None) -> bool:
    """Open AND within its TTL. The single definition of 'a session that may
    still shop' — used by the storefront and every cart mutation."""
    if session is None or session.status != "open":
        return False
    created = session.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created + PUNCHOUT_SESSION_TTL >= datetime.now(timezone.utc)


@router.get("/oci/start")
@router.post("/oci/start")
async def oci_start(
    request: Request,
    tenant_id: str = Query("demo"),
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
):
    """Entry point SAP calls. Accepts HOOK_URL (and the punchout secret) as
    query or form fields — SPRO call structures can send either."""
    params = dict(request.query_params)
    if request.method == "POST":
        form = await request.form()
        params.update({k: str(v) for k, v in form.items()})

    # --- Front door: right tenant + right credential, or a same-tenant staff
    # login. Checked before HOOK_URL so a stranger probing the endpoint learns
    # nothing about parameter shapes past the 401.
    tenant = db.get(Tenant, tenant_id)
    secret = params.get("PASSWORD") or params.get("punchout_secret") or ""
    staff = staff_user_from_header(db, authorization)
    # Always hash the supplied secret and run compare_digest against a same-
    # length target, so unknown-tenant / no-secret / wrong-secret take uniform
    # time — the identical 401 body isn't undercut by a timing side channel.
    supplied_hash = hashlib.sha256(secret.encode()).hexdigest()
    stored_hash = tenant.punchout_secret_hash if tenant is not None else None
    secret_ok = hmac.compare_digest(
        supplied_hash, stored_hash or _DUMMY_SECRET_HASH
    ) and stored_hash is not None
    staff_ok = staff is not None and staff.tenant_id == tenant_id and tenant is not None
    if not (staff_ok or secret_ok):
        raise HTTPException(401, _FRONT_DOOR_401)

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
    # session descriptor as before. The session id is the shopper's key from
    # here on; the secret never appears in a URL the requisitioner can see.
    if "text/html" in (request.headers.get("accept") or ""):
        return RedirectResponse(f"/shop?session={session.id}", status_code=302)

    return {
        "session_id": session.id,
        "oci_version": session.oci_version,
        "storefront": f"/api/catalog/items?session={session.id}",
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
    if not session_is_live(session):
        raise HTTPException(409, "Session is closed or expired — start a new punchout")

    # published_only: a rejected / in-review / superseded part must never be
    # priced and pushed to SAP, even by a valid session that knows its part id.
    item = _find_item(db, session.tenant_id, body.part, published_only=True)
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
    if not session_is_live(session):
        raise HTTPException(409, "Session is closed or expired — start a new punchout")
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


_NEW_ITEM = re.compile(r"^NEW_ITEM-([A-Z_]+)\[(\d+)\]$")


@router.post("/oci/mock-requisition", response_class=HTMLResponse)
async def mock_requisition(request: Request):
    """Same test double, rendered as a requisition instead of JSON.

    Point HOOK_URL here to demonstrate the round trip on a machine with no
    SAP access: the cart posts real OCI fields and this page shows the lines
    the way a requisition would carry them. Clearly labelled as a simulation —
    it is a receiver for demos, never a claim to be SAP.
    """
    form = await request.form()
    lines: dict[int, dict[str, str]] = {}
    other: dict[str, str] = {}
    for key, value in form.items():
        m = _NEW_ITEM.match(key)
        if m:
            field, idx = m.group(1), int(m.group(2))
            lines.setdefault(idx, {})[field] = str(value)
        else:
            other[key] = str(value)

    def cell(row: dict, field: str) -> str:
        return html.escape(row.get(field, ""))

    rows = "\n".join(
        f"""<tr><td class="mono">{n:03d}0</td>
        <td class="mono">{cell(row, 'VENDORMAT')}</td>
        <td>{cell(row, 'DESCRIPTION')}</td>
        <td class="mono num">{cell(row, 'QUANTITY')}</td>
        <td class="mono">{cell(row, 'UNIT')}</td>
        <td class="mono num">{cell(row, 'PRICE')}</td>
        <td class="mono num">{cell(row, 'PRICEUNIT')}</td>
        <td class="mono">{cell(row, 'CURRENCY')}</td>
        <td class="mono">{cell(row, 'VENDOR') or '&mdash;'}</td>
        <td class="mono num">{cell(row, 'LEADTIME')}</td></tr>"""
        for n, row in sorted(lines.items())
    )
    extras = "".join(
        f"<div><span class='mono'>{html.escape(k)}</span> = "
        f"<span class='mono'>{html.escape(v)}</span></div>"
        for k, v in sorted(other.items())
    )
    raw = "\n".join(
        f"{html.escape(k)} = {html.escape(str(v))}" for k, v in sorted(form.items())
    )

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Simulated SAP receiver — requisition items</title>
<style>
 body{{margin:0;background:#E7EAE4;color:#13171B;
   font:400 15px/1.55 "IBM Plex Sans",Segoe UI,system-ui,sans-serif}}
 .wrap{{max-width:1100px;margin:0 auto;padding:26px 24px 60px}}
 .banner{{background:#FBF3D9;border:1px solid #D8C47E;color:#7A5E0E;
   padding:10px 14px;font-size:.85rem;margin-bottom:22px}}
 h1{{font-size:1.4rem;margin:0 0 4px}}
 .sub{{color:#585F63;font-size:.9rem;margin-bottom:20px}}
 .card{{background:#F4F5F1;border:1px solid #C6CBC2;padding:18px;margin-bottom:20px}}
 h2{{font-size:1rem;margin:0 0 12px}}
 table{{width:100%;border-collapse:collapse;font-size:.85rem}}
 th{{font-size:.62rem;letter-spacing:.13em;text-transform:uppercase;color:#585F63;
   text-align:left;padding:8px 10px;border-bottom:1px solid #C6CBC2;white-space:nowrap}}
 td{{padding:8px 10px;border-bottom:1px solid #C6CBC2}}
 .mono{{font-family:"IBM Plex Mono",Consolas,ui-monospace,monospace}}
 .num{{text-align:right;font-variant-numeric:tabular-nums}}
 .scroll{{overflow-x:auto}}
 pre{{background:#fff;border:1px solid #C6CBC2;padding:12px;font-size:.76rem;
   overflow:auto;max-height:320px;margin:0}}
 summary{{cursor:pointer;font-size:.85rem;color:#585F63}}
</style></head><body><div class="wrap">
 <div class="banner"><b>Simulated SAP receiver.</b> This page stands in for
 SAP's <span class="mono">HOOK_URL</span>. These are the exact OCI fields a real
 S/4HANA system receives from the punchout transfer — nothing here is SAP.</div>
 <h1>Requisition items received</h1>
 <div class="sub">{len(lines)} line(s) transferred from the Interlock catalog.</div>
 <div class="card"><h2>Item overview</h2><div class="scroll"><table>
   <thead><tr><th>Item</th><th>Vendor mat.</th><th>Short text</th><th>Qty</th>
   <th>Un</th><th>Net price</th><th>Per</th><th>Crcy</th><th>Vendor</th>
   <th>Lead t.</th></tr></thead>
   <tbody>{rows or '<tr><td colspan="10">No NEW_ITEM fields in the post.</td></tr>'}</tbody>
 </table></div></div>
 {f'<div class="card"><h2>Other fields</h2>{extras}</div>' if extras else ''}
 <div class="card"><h2>Raw OCI payload</h2>
   <details><summary>Show every field exactly as posted</summary>
   <pre>{raw or '(empty)'}</pre></details></div>
</div></body></html>"""
    return HTMLResponse(page)


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

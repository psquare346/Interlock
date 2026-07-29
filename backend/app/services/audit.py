"""The leakage audit — the sales weapon.

Input: a PO-history spreadsheet any SAP user can export in minutes
(columns: po_number, date, part, quantity, unit_price — header names are
matched loosely). For every line we resolve what the contract said the price
should have been on that date and total the difference in dollars.

Runs as a background job (kind "leakage_audit") because a year of PO lines
must never sit inside an HTTP request. The summary lands on the job row; the
line-by-line detail CSV goes through the storage seam.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from . import storage
from .jobs import handler
from .pricing import resolve_price

# Loose header matching: first alias found wins (lowercased, stripped).
_ALIASES = {
    "po_number": ["po_number", "po", "purchase_order", "ebeln", "po no", "po_no"],
    "date": ["date", "po_date", "order_date", "doc_date", "bedat"],
    "part": ["part", "supplier_part_id", "part_id", "material", "vendor_material",
             "part_number", "item", "idnlf"],
    "quantity": ["quantity", "qty", "menge"],
    "unit_price": ["unit_price", "price", "net_price", "price_paid", "paid", "netpr"],
}


class AuditError(Exception):
    pass


def _map_headers(headers: list[str]) -> dict[str, str]:
    normalized = {h.strip().lower().replace(" ", "_"): h for h in headers}
    mapping = {}
    for field, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[field] = normalized[alias]
                break
    missing = [f for f in ("po_number", "part", "quantity", "unit_price") if f not in mapping]
    if missing:
        raise AuditError(
            f"Could not find columns for: {', '.join(missing)}. "
            f"Headers seen: {headers}"
        )
    return mapping


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%Y%m%d"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(raw[:10] if fmt != "%Y%m%d" else raw[:8], fmt).date()
        except ValueError:
            continue
    return None


@handler("leakage_audit")
def run_leakage_audit(db: Session, payload: dict) -> dict:
    """payload: {"storage_key": ..., "tenant_id": ..., "filename": ...}"""
    from ..api.pricing import _find_item  # shared published-item lookup

    tenant_id = payload["tenant_id"]
    raw = storage.get(payload["storage_key"])
    text = raw.decode("utf-8-sig", errors="replace")

    # Sniff the delimiter the same way ingestion does (comma vs semicolon).
    delimiter = ";" if text.splitlines()[0].count(";") > text.splitlines()[0].count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise AuditError("The file has no header row")
    cols = _map_headers(list(reader.fieldnames))

    detail_rows: list[dict] = []
    totals = {
        "lines": 0, "matched": 0, "off_catalog": 0, "unparseable": 0,
        "spend": Decimal("0"), "contract_spend": Decimal("0"),
        "leakage": Decimal("0"), "underpaid": Decimal("0"),
    }
    by_part: dict[str, Decimal] = {}

    for row in reader:
        totals["lines"] += 1
        part = (row.get(cols["part"]) or "").strip()
        try:
            qty = Decimal((row.get(cols["quantity"]) or "1").replace(",", "") or "1")
            paid = Decimal((row.get(cols["unit_price"]) or "").replace(",", "").replace("$", ""))
        except InvalidOperation:
            totals["unparseable"] += 1
            continue
        on = _parse_date(row.get(cols["date"])) if "date" in cols else None
        spend = paid * qty
        totals["spend"] += spend

        item = _find_item(db, tenant_id, part) if part else None
        if item is None:
            totals["off_catalog"] += 1
            detail_rows.append({
                "po_number": row.get(cols["po_number"]), "part": part,
                "date": on.isoformat() if on else "", "quantity": str(qty),
                "paid_unit_price": str(paid), "contract_unit_price": "",
                "verdict": "off_catalog", "leakage": "",
            })
            continue

        resolved = resolve_price(db, item, int(qty) or 1, on=on)
        expected = resolved.effective_unit_price
        totals["matched"] += 1
        totals["contract_spend"] += expected * qty
        delta = (paid - expected) * qty
        if delta > 0:
            totals["leakage"] += delta
            by_part[part] = by_part.get(part, Decimal("0")) + delta
            verdict = "overpaid"
        elif delta < 0:
            totals["underpaid"] += -delta
            verdict = "underpaid"
        else:
            verdict = "match"
        detail_rows.append({
            "po_number": row.get(cols["po_number"]), "part": part,
            "date": on.isoformat() if on else "", "quantity": str(qty),
            "paid_unit_price": str(paid), "contract_unit_price": str(expected),
            "verdict": verdict,
            "leakage": str(delta.quantize(Decimal("0.01"))) if delta > 0 else "",
        })

    # Detail report through the storage seam.
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=[
        "po_number", "part", "date", "quantity", "paid_unit_price",
        "contract_unit_price", "verdict", "leakage",
    ])
    writer.writeheader()
    writer.writerows(detail_rows)
    report_key = f"{tenant_id}/audits/{payload.get('job_id', 'report')}.csv"
    storage.put(report_key, out.getvalue().encode("utf-8"))

    top = sorted(by_part.items(), key=lambda kv: kv[1], reverse=True)[:10]
    money = lambda d: float(d.quantize(Decimal("0.01")))
    return {
        "filename": payload.get("filename"),
        "lines": totals["lines"],
        "matched_lines": totals["matched"],
        "off_catalog_lines": totals["off_catalog"],
        "unparseable_lines": totals["unparseable"],
        "total_spend": money(totals["spend"]),
        "contracted_spend_on_matched": money(totals["contract_spend"]),
        "leakage_total": money(totals["leakage"]),
        "underpaid_total": money(totals["underpaid"]),
        "leakage_pct_of_spend": (
            money(totals["leakage"] / totals["spend"] * 100) if totals["spend"] else 0.0
        ),
        "top_offenders": [{"part": p, "leakage": money(d)} for p, d in top],
        "report_key": report_key,
    }

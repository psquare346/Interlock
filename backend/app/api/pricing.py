"""Price tiers: upload a ladder file, resolve a price."""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.responses import FileResponse

TEMPLATES = Path(__file__).resolve().parent.parent.parent / "samples" / "templates"

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import CatalogItem, Contract, ItemState, PriceTier, User
from ..services.auth import require
from ..services.pricing import (
    ParsedTier, parse_encoded_string, resolve_price, validate_ladder,
)

router = APIRouter()


def _find_item(db: Session, tenant_id: str, part: str) -> CatalogItem | None:
    """Latest decided row for a part, preferring published-eligible states."""
    rows = db.scalars(
        select(CatalogItem).where(
            CatalogItem.tenant_id == tenant_id,
            CatalogItem.supplier_part_id == part,
        )
    ).all()
    usable = [r for r in rows if r.state in (ItemState.AUTO_APPROVED, ItemState.APPROVED)]
    pool = usable or rows
    return max(pool, key=lambda r: r.version.version_no) if pool else None


@router.get("/tiers/template")
def tiers_template():
    """Download the tier pricing template: one line per tier, grouped by
    repeating the part id; the quantity range IS the tier."""
    return FileResponse(
        TEMPLATES / "price_tiers_template.csv",
        media_type="text/csv",
        filename="price_tiers_template.csv",
    )


@router.post("/tiers/upload", status_code=201)
def upload_tiers(
    file: UploadFile = File(...),
    contract_no: str = Form(...),
    valid_from: date | None = Form(None),
    valid_to: date | None = Form(None),
    user: User = Depends(require("pricing_manage")),
    db: Session = Depends(get_db),
):
    tenant_id = user.tenant_id
    """Long-format CSV: supplier_part_id,min_qty,max_qty,unit_price[,price_unit][,currency]
    or encoded: supplier_part_id,tiers  with "1-9:42.00;10-49:38.50;50+:35.00".
    """
    contract = db.scalars(
        select(Contract).where(
            Contract.tenant_id == tenant_id, Contract.contract_no == contract_no
        )
    ).first()
    if contract is None:
        raise HTTPException(404, f"Contract {contract_no!r} not found")

    text = file.file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(422, "Empty file")
    fields = {f.strip().lower() for f in reader.fieldnames}

    # Group parsed tiers per part, then validate each ladder as a whole.
    ladders: dict[str, list[ParsedTier]] = {}
    errors: list[str] = []

    for line_no, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        part = row.get("supplier_part_id") or row.get("part")
        if not part:
            errors.append(f"Line {line_no}: missing supplier_part_id")
            continue

        if "tiers" in fields:  # encoded-string shape
            parsed = parse_encoded_string(row.get("tiers", ""), currency=contract.currency)
            if not parsed.ok:
                errors.extend(f"Line {line_no}: {e}" for e in parsed.errors)
                continue
            ladders.setdefault(part, []).extend(parsed.tiers)
        else:  # long shape
            try:
                ladders.setdefault(part, []).append(
                    ParsedTier(
                        min_qty=int(row["min_qty"]),
                        max_qty=int(row["max_qty"]) if row.get("max_qty") else None,
                        unit_price=Decimal(row["unit_price"]),
                        price_unit=int(row.get("price_unit") or 1),
                        currency=(row.get("currency") or contract.currency).upper(),
                    )
                )
            except (KeyError, ValueError, ArithmeticError) as e:
                errors.append(f"Line {line_no}: {e!r}")

    created = 0
    per_part: dict[str, list[str]] = {}
    for part, tiers in ladders.items():
        ladder_errors = validate_ladder(tiers, contract, valid_from, valid_to)
        item = _find_item(db, tenant_id, part)
        if item is None:
            ladder_errors.append(f"No catalog item {part!r} in tenant {tenant_id!r}")
        if ladder_errors:
            per_part[part] = ladder_errors
            continue
        for t in tiers:
            db.add(
                PriceTier(
                    tenant_id=tenant_id,
                    item_id=item.id,
                    contract_id=contract.id,
                    min_qty=t.min_qty,
                    max_qty=t.max_qty,
                    unit_price=t.unit_price,
                    price_unit=t.price_unit,
                    currency=t.currency,
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
            )
            created += 1
    db.commit()

    return {
        "contract_no": contract_no,
        "parts_loaded": len(ladders) - len(per_part),
        "tiers_created": created,
        "parts_rejected": per_part,
        "row_errors": errors,
    }


@router.get("/resolve")
def resolve(
    part: str = Query(...),
    quantity: int = Query(1, ge=1),
    tenant_id: str = Query("demo"),
    on: date | None = Query(None),
    db: Session = Depends(get_db),
):
    item = _find_item(db, tenant_id, part)
    if item is None:
        raise HTTPException(404, f"No catalog item {part!r}")
    r = resolve_price(db, item, quantity, on=on)
    return {
        "part": part,
        "quantity": quantity,
        "unit_price": float(r.unit_price),
        "price_unit": r.price_unit,
        "effective_unit_price": float(r.effective_unit_price),
        "extended": float(r.extended),
        "currency": r.currency,
        "source": r.source,
        "contract_id": r.contract_id,
        "next_break": r.next_break,
    }

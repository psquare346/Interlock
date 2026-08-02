"""Catalog: upload, review queue, decide, publish."""

from __future__ import annotations

from datetime import datetime, timezone

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

TEMPLATES = Path(__file__).resolve().parent.parent.parent / "samples" / "templates"

from ..db import get_db
from ..models import (
    CatalogItem, CatalogVersion, ItemState, PunchoutSession, Supplier, User,
    VersionStatus,
)
from ..services import ingestion
from ..services.auth import get_current_user, require, staff_user_from_header

router = APIRouter()


def _own_version(db: Session, user: User, version_id: str) -> CatalogVersion:
    version = db.get(CatalogVersion, version_id)
    if version is None or version.tenant_id != user.tenant_id:
        raise HTTPException(404, "Version not found")
    return version


def _get_supplier(db: Session, tenant_id: str, code: str) -> Supplier:
    supplier = db.scalars(
        select(Supplier).where(Supplier.tenant_id == tenant_id, Supplier.code == code)
    ).first()
    if supplier is None:
        raise HTTPException(404, f"Supplier {code!r} not found in tenant {tenant_id!r}")
    return supplier


def _item_out(i: CatalogItem) -> dict:
    return {
        "id": i.id,
        "supplier_part_id": i.supplier_part_id,
        "description": i.description,
        "uom_raw": i.uom_raw,
        "uom_sap": i.uom_sap,
        "unspsc": i.unspsc,
        "material_group": i.material_group,
        "unit_price": float(i.unit_price) if i.unit_price is not None else None,
        "price_unit": i.price_unit,
        "currency": i.currency,
        "state": i.state.value,
        "confidence": i.confidence,
        "review_reasons": i.review_reasons,
        "price_flags": i.price_flags,
    }


@router.get("/template")
def catalog_template():
    """Download the canonical catalog upload template. Using these exact
    headers guarantees a 100%-confidence column mapping."""
    return FileResponse(
        TEMPLATES / "catalog_template.csv",
        media_type="text/csv",
        filename="catalog_template.csv",
    )


@router.post("/upload", status_code=201)
def upload_catalog(
    file: UploadFile = File(...),
    supplier_code: str = Form(...),
    user: User = Depends(require("catalog_upload")),
    db: Session = Depends(get_db),
):
    supplier = _get_supplier(db, user.tenant_id, supplier_code)
    content = file.file.read()
    try:
        version = ingestion.ingest(db, user.tenant_id, supplier, file.filename or "upload.csv", content)
    except ingestion.IngestError as e:
        raise HTTPException(422, str(e))

    counts: dict[str, int] = {}
    for item in version.items:
        counts[item.state.value] = counts.get(item.state.value, 0) + 1

    return {
        "version_id": version.id,
        "version_no": version.version_no,
        "supplier_code": supplier.code,
        "row_count": version.row_count,
        "states": counts,
        "mapping": version.mapping_used,
        "next": f"/api/catalog/versions/{version.id}/review",
    }


@router.get("/versions")
def list_versions(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    versions = db.scalars(
        select(CatalogVersion)
        .where(CatalogVersion.tenant_id == user.tenant_id)
        .order_by(CatalogVersion.created_at.desc())
    ).all()
    out = []
    for v in versions:
        supplier = db.get(Supplier, v.supplier_id)
        counts: dict[str, int] = {}
        for item in v.items:
            counts[item.state.value] = counts.get(item.state.value, 0) + 1
        out.append({
            "version_id": v.id,
            "version_no": v.version_no,
            "supplier_code": supplier.code if supplier else None,
            "status": v.status.value,
            "source_filename": v.source_filename,
            "row_count": v.row_count,
            "states": counts,
            "created_at": str(v.created_at),
        })
    return out


class ItemEdit(BaseModel):
    supplier_part_id: str | None = None
    description: str | None = None
    long_description: str | None = None
    uom_raw: str | None = None
    unspsc: str | None = None
    material_group: str | None = None
    unit_price: float | None = None
    price_unit: int | None = None
    currency: str | None = None


@router.patch("/items/{item_id}")
def edit_item(
    item_id: str,
    body: ItemEdit,
    user: User = Depends(require("catalog_review")),
    db: Session = Depends(get_db),
):
    """Fix a queued row in place, then re-validate with the same hard rules
    ingestion used. A passing edit lands in needs_review — the approve click
    is still a separate human decision."""
    actor = user.email
    item = db.get(CatalogItem, item_id)
    if item is None or item.tenant_id != user.tenant_id:
        raise HTTPException(404, "Item not found")
    if item.version.status is not VersionStatus.LOADED:
        raise HTTPException(409, "Version is already published; load a new version instead")

    changed = body.model_dump(exclude_unset=True)
    if not changed:
        raise HTTPException(422, "No fields to change")
    for field_name, value in changed.items():
        setattr(item, field_name, value)
    if "description" in changed and item.description:
        if len(item.description) > 40:
            item.long_description = item.long_description or item.description
            item.description = item.description[:40]
    if "currency" in changed and item.currency:
        item.currency = item.currency.upper()

    hard = ingestion.revalidate_item(db, item)
    edit_note = f"Edited by {actor}: {', '.join(sorted(changed))}"
    if hard:
        item.state = ItemState.MANUAL
        item.review_reasons = hard + [edit_note]
        item.confidence = 0.0
    else:
        item.state = ItemState.NEEDS_REVIEW
        item.review_reasons = [edit_note]
        item.confidence = None

    db.commit()
    return _item_out(item)


@router.get("/versions/{version_id}/review")
def review_queue(
    version_id: str,
    state: str | None = Query(None, description="Filter: needs_review | manual | auto_approved | approved | rejected"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    version = _own_version(db, user, version_id)

    items = version.items
    if state:
        try:
            wanted = ItemState(state)
        except ValueError:
            raise HTTPException(422, f"Unknown state {state!r}")
        items = [i for i in items if i.state is wanted]

    return {
        "version_id": version.id,
        "version_no": version.version_no,
        "status": version.status.value,
        "mapping": version.mapping_used,
        "items": [_item_out(i) for i in items],
    }


@router.post("/items/{item_id}/decide")
def decide_item(
    item_id: str,
    decision: str = Query(..., pattern="^(approve|reject)$"),
    user: User = Depends(require("catalog_review")),
    db: Session = Depends(get_db),
):
    actor = user.email
    item = db.get(CatalogItem, item_id)
    if item is None or item.tenant_id != user.tenant_id:
        raise HTTPException(404, "Item not found")
    if item.version.status is not VersionStatus.LOADED:
        raise HTTPException(409, "Version is already published; load a new version to change items")

    item.state = ItemState.APPROVED if decision == "approve" else ItemState.REJECTED
    item.decided_by = actor
    item.decided_at = datetime.now(timezone.utc)
    db.commit()
    return _item_out(item)


@router.post("/versions/{version_id}/confirm-mapping")
def confirm_mapping(
    version_id: str,
    user: User = Depends(require("catalog_review")),
    db: Session = Depends(get_db),
):
    """Persist this load's mapping against the supplier forever.

    Load #2 onward then skips the mapping step entirely — the whole point.
    """
    version = _own_version(db, user, version_id)
    supplier = db.get(Supplier, version.supplier_id)
    supplier.confirmed_mapping = {
        "mappings": (version.mapping_used or {}).get("mappings", []),
        "confirmed_by": user.email,
    }
    db.commit()
    return {"supplier_code": supplier.code, "confirmed": True}


@router.post("/versions/{version_id}/publish")
def publish_version(
    version_id: str,
    user: User = Depends(require("catalog_publish")),
    db: Session = Depends(get_db),
):
    actor = user.email
    version = _own_version(db, user, version_id)
    if version.status is not VersionStatus.LOADED:
        raise HTTPException(409, f"Version is {version.status.value}, not loaded")

    blocking = [
        i for i in version.items
        if i.state in (ItemState.NEEDS_REVIEW, ItemState.MANUAL)
    ]
    if blocking:
        raise HTTPException(
            409,
            f"{len(blocking)} item(s) still queued for review. Decide them "
            f"(approve/reject) before publishing.",
        )

    # Supersede the previous published version for this supplier.
    for prior in db.scalars(
        select(CatalogVersion).where(
            CatalogVersion.supplier_id == version.supplier_id,
            CatalogVersion.status == VersionStatus.PUBLISHED,
        )
    ).all():
        prior.status = VersionStatus.SUPERSEDED

    version.status = VersionStatus.PUBLISHED
    version.published_at = datetime.now(timezone.utc)
    version.published_by = actor
    db.commit()

    published = [
        i for i in version.items
        if i.state in (ItemState.AUTO_APPROVED, ItemState.APPROVED)
    ]
    return {
        "version_id": version.id,
        "status": version.status.value,
        "published_items": len(published),
        "rejected_items": sum(1 for i in version.items if i.state is ItemState.REJECTED),
    }


@router.get("/items")
def search_items(
    session: str | None = Query(None, description="Open punchout session id"),
    q: str | None = Query(None, description="Substring match on part id / description"),
    tenant_id: str | None = Query(None, deprecated=True,
                                  description="Ignored — tenant comes from the "
                                  "session or the login, never the caller"),
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
):
    """Published items only — what the punchout storefront sees.

    Callers prove which tenant they are instead of naming one: a shopper
    carries an open punchout session id (minted by the credentialed
    /oci/start), staff carry their bearer token. The tenant_id parameter is
    accepted for backward compatibility but never trusted."""
    from .punchout import session_is_live  # local import avoids an import cycle

    resolved_tenant: str | None = None
    if session:
        ps = db.get(PunchoutSession, session)
        if session_is_live(ps):
            resolved_tenant = ps.tenant_id
    if resolved_tenant is None and authorization:
        staff = staff_user_from_header(db, authorization)
        if staff is not None:
            resolved_tenant = staff.tenant_id
    if resolved_tenant is None:
        raise HTTPException(
            401, "Catalog access needs an open punchout session or a staff login"
        )

    versions = db.scalars(
        select(CatalogVersion).where(
            CatalogVersion.tenant_id == resolved_tenant,
            CatalogVersion.status == VersionStatus.PUBLISHED,
        )
    ).all()
    out = []
    for v in versions:
        for i in v.items:
            if i.state not in (ItemState.AUTO_APPROVED, ItemState.APPROVED):
                continue
            if q:
                hay = f"{i.supplier_part_id} {i.description} {i.long_description or ''}".lower()
                if q.lower() not in hay:
                    continue
            out.append(_item_out(i))
    return out

"""Policy: upload (YAML -> DRAFT), preview the clamp, activate, evaluate."""

from __future__ import annotations

from datetime import date

import yaml
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Contract, Policy, User
from ..services.auth import get_current_user, require
from ..services.policy import (
    PolicyError, activate_policy, compute_effective_window, evaluate_line,
    expire_lapsed_policies, load_policy_from_dict,
)

router = APIRouter()


def _policy_out(p: Policy) -> dict:
    return {
        "id": p.id,
        "code": p.code,
        "name": p.name,
        "version_no": p.version_no,
        "status": p.status.value,
        "requested_from": str(p.requested_from),
        "requested_to": str(p.requested_to),
        "effective_from": str(p.effective_from) if p.effective_from else None,
        "effective_to": str(p.effective_to) if p.effective_to else None,
        "clamped": p.clamped,
        "clamp_note": p.clamp_note,
        "rules": [
            {
                "code": r.code,
                "description": r.description,
                "action": r.action.value,
                "route_to": r.route_to,
                "severity": r.severity,
                "scope": r.scope,
                "condition": r.condition,
            }
            for r in p.rules
        ],
    }


@router.post("/upload", status_code=201)
def upload_policy(
    file: UploadFile = File(...),
    user: User = Depends(require("policy_manage")),
    db: Session = Depends(get_db),
):
    tenant_id, actor = user.tenant_id, user.email
    try:
        payload = yaml.safe_load(file.file.read())
    except yaml.YAMLError as e:
        raise HTTPException(422, f"Not valid YAML: {e}")
    if not isinstance(payload, dict):
        raise HTTPException(422, "Policy file must be a YAML mapping")

    try:
        policy = load_policy_from_dict(db, tenant_id, payload, actor)
    except PolicyError as e:
        raise HTTPException(422, str(e))
    return _policy_out(policy)


@router.get("/{policy_id}/preview-window")
def preview_window(
    policy_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """What would activation grant? Shows the clamp without committing."""
    policy = db.get(Policy, policy_id)
    if policy is None or policy.tenant_id != user.tenant_id:
        raise HTTPException(404, "Policy not found")
    contract = db.get(Contract, policy.contract_id) if policy.contract_id else None
    try:
        eff_from, eff_to, clamped, note = compute_effective_window(
            policy.requested_from, policy.requested_to, contract
        )
    except PolicyError as e:
        raise HTTPException(422, str(e))
    return {
        "requested": [str(policy.requested_from), str(policy.requested_to)],
        "effective": [str(eff_from), str(eff_to)],
        "clamped": clamped,
        "note": note,
    }


@router.post("/{policy_id}/activate")
def activate(
    policy_id: str,
    user: User = Depends(require("policy_manage")),
    db: Session = Depends(get_db),
):
    policy = db.get(Policy, policy_id)
    if policy is None or policy.tenant_id != user.tenant_id:
        raise HTTPException(404, "Policy not found")
    try:
        policy = activate_policy(db, policy, user.email)
    except PolicyError as e:
        raise HTTPException(422, str(e))
    return _policy_out(policy)


@router.post("/evaluate")
def evaluate(
    context: dict = Body(...),
    on: date | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = evaluate_line(db, user.tenant_id, context, on=on)
    return result.to_dict()


@router.post("/expire-lapsed")
def expire(
    user: User = Depends(require("policy_manage")),
    db: Session = Depends(get_db),
):
    """Run the scheduled expiry sweep by hand."""
    return {"expired": expire_lapsed_policies(db)}


@router.get("")
def list_policies(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Policy).where(Policy.tenant_id == user.tenant_id).order_by(Policy.code, Policy.version_no)
    ).all()
    return [_policy_out(p) for p in rows]

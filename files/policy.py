"""Procurement policy: activation with contract clamping, and deterministic
rule evaluation.

No model is in the request path. A model may draft rules from a written policy
document, and a model may phrase an explanation after the fact. Neither decides
anything. A model that evaluates policy at runtime will eventually approve a
$400,000 single-source purchase because the request was worded persuasively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ACTION_SEVERITY, Contract, Policy, PolicyAction, PolicyEvaluation,
    PolicyRule, PolicyStatus,
)


class PolicyError(Exception):
    """Raised when an activation would produce an invalid policy window."""


# --------------------------------------------------------------------------
# Activation — the contract clamp
# --------------------------------------------------------------------------

def compute_effective_window(
    requested_from: date,
    requested_to: date,
    contract: Contract | None,
) -> tuple[date, date, bool, str | None]:
    """Clamp a requested window to the bound contract's validity.

        effective_from = max(requested_from, contract.valid_from)
        effective_to   = min(requested_to,   contract.valid_to)

    Returns (effective_from, effective_to, was_clamped, note).
    Raises PolicyError if the windows do not overlap at all.
    """
    if requested_from > requested_to:
        raise PolicyError(
            f"Requested window is inverted: {requested_from} to {requested_to}"
        )

    if contract is None:
        return requested_from, requested_to, False, None

    eff_from = max(requested_from, contract.valid_from)
    eff_to = min(requested_to, contract.valid_to)

    if eff_from > eff_to:
        raise PolicyError(
            f"Requested window {requested_from}..{requested_to} falls entirely "
            f"outside contract {contract.contract_no} "
            f"({contract.valid_from}..{contract.valid_to}). "
            "This is an error, not a warning — pick dates inside the contract."
        )

    clamped = (eff_from != requested_from) or (eff_to != requested_to)
    note = None
    if clamped:
        parts = []
        if eff_from != requested_from:
            parts.append(f"start moved {requested_from} → {eff_from}")
        if eff_to != requested_to:
            parts.append(f"end moved {requested_to} → {eff_to}")
        note = (
            f"Clamped to contract {contract.contract_no} "
            f"({contract.valid_from}..{contract.valid_to}): " + ", ".join(parts)
        )
    return eff_from, eff_to, clamped, note


def activate_policy(db: Session, policy: Policy, actor: str) -> Policy:
    """Activate a draft policy.

    This is the only transition that runs the clamp. It also supersedes any
    currently active version of the same policy code whose window would overlap.
    """
    if policy.status is not PolicyStatus.DRAFT:
        raise PolicyError(
            f"Only draft policies can be activated; this one is {policy.status.value}"
        )
    if not policy.rules:
        raise PolicyError("Cannot activate a policy with no rules")

    contract = None
    if policy.contract_id:
        contract = db.get(Contract, policy.contract_id)
        if contract is None:
            raise PolicyError(f"Bound contract {policy.contract_id} not found")
        if not contract.active:
            raise PolicyError(
                f"Contract {contract.contract_no} is inactive — reactivate it first"
            )

    eff_from, eff_to, clamped, note = compute_effective_window(
        policy.requested_from, policy.requested_to, contract
    )

    # Two active versions of one policy code may never overlap in time.
    existing = db.scalars(
        select(Policy).where(
            Policy.tenant_id == policy.tenant_id,
            Policy.code == policy.code,
            Policy.status == PolicyStatus.ACTIVE,
        )
    ).all()

    for other in existing:
        if other.effective_from is None or other.effective_to is None:
            continue
        overlaps = not (eff_to < other.effective_from or eff_from > other.effective_to)
        if overlaps:
            other.status = PolicyStatus.SUPERSEDED
            db.add(other)

    policy.effective_from = eff_from
    policy.effective_to = eff_to
    policy.clamped = clamped
    policy.clamp_note = note
    policy.status = PolicyStatus.ACTIVE
    policy.activated_by = actor
    policy.activated_at = datetime.now(timezone.utc)
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


def expire_lapsed_policies(db: Session, on: date | None = None) -> int:
    """Expire active policies whose window has passed, or whose contract has.

    Run this on a schedule. Extending a contract deliberately does *not*
    resurrect its policies — that requires a new version, so a human looks again.
    """
    on = on or date.today()
    count = 0
    for policy in db.scalars(select(Policy).where(Policy.status == PolicyStatus.ACTIVE)).all():
        lapsed = policy.effective_to is not None and policy.effective_to < on
        if not lapsed and policy.contract_id:
            contract = db.get(Contract, policy.contract_id)
            if contract and (not contract.active or contract.valid_to < on):
                lapsed = True
        if lapsed:
            policy.status = PolicyStatus.EXPIRED
            db.add(policy)
            count += 1
    if count:
        db.commit()
    return count


# --------------------------------------------------------------------------
# Rule evaluation
# --------------------------------------------------------------------------

_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a is not None and a > b,
    "gte": lambda a, b: a is not None and a >= b,
    "lt": lambda a, b: a is not None and a < b,
    "lte": lambda a, b: a is not None and a <= b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "contains": lambda a, b: b in (a or ""),
    "starts_with": lambda a, b: str(a or "").startswith(str(b)),
    "between": lambda a, b: a is not None and b[0] <= a <= b[1],
}


def _evaluate_condition(node: dict, ctx: dict[str, Any]) -> bool:
    """Evaluate a condition tree.

    Grammar:
        {"all": [...]}  {"any": [...]}  {"none": [...]}
        {"field": "line_total", "op": "gt", "value": 5000}
    """
    if not node:
        return False

    if "all" in node:
        return all(_evaluate_condition(c, ctx) for c in node["all"])
    if "any" in node:
        return any(_evaluate_condition(c, ctx) for c in node["any"])
    if "none" in node:
        return not any(_evaluate_condition(c, ctx) for c in node["none"])

    field_name = node.get("field")
    op = node.get("op")
    expected = node.get("value")
    if field_name is None or op not in _OPS:
        return False

    actual = ctx.get(field_name)
    try:
        return bool(_OPS[op](actual, expected))
    except (TypeError, ValueError):
        # A comparison that cannot be made is a non-match, never a match.
        return False


# Scope keys read naturally in the plural ("company_codes: [1000]") while the
# evaluation context is per-line and therefore singular ("company_code": "1000").
# Without this map every scoped rule silently fails to match, which is a
# spectacularly quiet way to disable a policy.
SCOPE_FIELD_MAP = {
    "company_codes": "company_code",
    "material_groups": "material_group",
    "plants": "plant",
    "suppliers": "supplier_code",
    "supplier_codes": "supplier_code",
    "purchasing_orgs": "purchasing_org",
    "cost_centers": "cost_center",
    "currencies": "currency",
}


def _in_scope(scope: dict, ctx: dict[str, Any]) -> bool:
    """Scope narrows where a rule applies. '*' or absent means everywhere.

    A scope key naming a field absent from the context is treated as
    out of scope, not in scope — rules fail closed.
    """
    for key, allowed in (scope or {}).items():
        if not allowed or allowed == ["*"] or allowed == "*":
            continue
        field_name = SCOPE_FIELD_MAP.get(key, key)
        value = ctx.get(field_name)
        if isinstance(allowed, list):
            if "*" in allowed:
                continue
            if value not in allowed:
                return False
        elif value != allowed:
            return False
    return True


@dataclass
class FiredRule:
    rule_code: str
    description: str
    action: PolicyAction
    route_to: str | None
    severity: str
    policy_code: str
    policy_version_no: int


@dataclass
class PolicyResult:
    outcome: PolicyAction
    fired: list[FiredRule] = field(default_factory=list)
    evaluated: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.outcome is PolicyAction.BLOCK

    @property
    def needs_approval(self) -> bool:
        return self.outcome is PolicyAction.ROUTE_APPROVAL

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "blocked": self.blocked,
            "needs_approval": self.needs_approval,
            "fired_rules": [
                {
                    "rule": f.rule_code,
                    "policy": f.policy_code,
                    "policy_version": f.policy_version_no,
                    "description": f.description,
                    "action": f.action.value,
                    "route_to": f.route_to,
                    "severity": f.severity,
                }
                for f in self.fired
            ],
        }


def evaluate_line(
    db: Session,
    tenant_id: str,
    context: dict[str, Any],
    on: date | None = None,
    cart_ref: str | None = None,
    line_ref: str | None = None,
    log: bool = True,
) -> PolicyResult:
    """Evaluate one cart line against every policy in force on ``on``.

    ``context`` carries the fields rules refer to, for example::

        {"line_total": 6400.00, "unit_price": 32.00, "quantity": 200,
         "material_group": "10203040", "company_code": "1000",
         "supplier_code": "ACME", "competitive_quotes": 1,
         "contract_days_remaining": 12, "is_contracted": True}

    Every rule that fires is recorded. The most severe action wins, but
    reviewers need the full picture, not just the verdict.
    """
    on = on or date.today()

    policies = [
        p for p in db.scalars(
            select(Policy).where(
                Policy.tenant_id == tenant_id,
                Policy.status == PolicyStatus.ACTIVE,
            )
        ).all()
        if p.is_effective_on(on)
    ]

    fired: list[FiredRule] = []

    for policy in policies:
        rules = db.scalars(
            select(PolicyRule).where(
                PolicyRule.policy_id == policy.id,
                PolicyRule.active.is_(True),
            )
        ).all()

        for rule in rules:
            if not _in_scope(rule.scope, context):
                continue
            if not _evaluate_condition(rule.condition, context):
                continue
            fired.append(
                FiredRule(
                    rule_code=rule.code,
                    description=rule.description,
                    action=rule.action,
                    route_to=rule.route_to,
                    severity=rule.severity,
                    policy_code=policy.code,
                    policy_version_no=policy.version_no,
                )
            )

    outcome = PolicyAction.ALLOW
    for f in fired:
        if ACTION_SEVERITY[f.action] > ACTION_SEVERITY[outcome]:
            outcome = f.action

    result = PolicyResult(outcome=outcome, fired=fired, evaluated=context)

    if log:
        db.add(
            PolicyEvaluation(
                tenant_id=tenant_id,
                cart_ref=cart_ref,
                line_ref=line_ref,
                policy_id=policies[0].id if policies else None,
                policy_version_no=policies[0].version_no if policies else None,
                fired_rules=[f.rule_code for f in fired],
                evaluated_fields=context,
                outcome=outcome.value,
            )
        )
        db.commit()

    return result


# --------------------------------------------------------------------------
# Loading rules from YAML
# --------------------------------------------------------------------------

def load_policy_from_dict(db: Session, tenant_id: str, payload: dict, actor: str) -> Policy:
    """Create a DRAFT policy from a parsed YAML/JSON definition.

    Always lands in DRAFT. There is no code path from a file — or a model —
    straight to ACTIVE. Activation is a separate, deliberate call.
    """
    required = {"code", "name", "requested_from", "requested_to", "rules"}
    missing = required - payload.keys()
    if missing:
        raise PolicyError(f"Policy definition is missing: {sorted(missing)}")

    contract_id = None
    if payload.get("contract_no"):
        contract = db.scalars(
            select(Contract).where(
                Contract.tenant_id == tenant_id,
                Contract.contract_no == payload["contract_no"],
            )
        ).first()
        if contract is None:
            raise PolicyError(f"Contract {payload['contract_no']!r} not found")
        contract_id = contract.id

    prior = db.scalars(
        select(Policy).where(Policy.tenant_id == tenant_id, Policy.code == payload["code"])
    ).all()
    next_version = max((p.version_no for p in prior), default=0) + 1

    policy = Policy(
        tenant_id=tenant_id,
        code=payload["code"],
        name=payload["name"],
        version_no=next_version,
        status=PolicyStatus.DRAFT,
        contract_id=contract_id,
        requested_from=_as_date(payload["requested_from"]),
        requested_to=_as_date(payload["requested_to"]),
        source_document=payload.get("source_document"),
        created_by=actor,
    )
    db.add(policy)
    db.flush()

    for raw in payload["rules"]:
        db.add(
            PolicyRule(
                tenant_id=tenant_id,
                policy_id=policy.id,
                code=raw["code"],
                description=raw.get("description", ""),
                scope=raw.get("scope", {}),
                condition=raw.get("condition", {}),
                action=PolicyAction(raw.get("action", "warn")),
                route_to=raw.get("route_to"),
                severity=raw.get("severity", "medium"),
                source_clause=raw.get("source_clause"),
                source_quote=raw.get("source_quote"),
                draft_confidence=raw.get("confidence"),
            )
        )

    db.commit()
    db.refresh(policy)
    return policy


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))

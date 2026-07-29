from datetime import date

import pytest

from app.models import (
    Contract, Policy, PolicyAction, PolicyRule, PolicyStatus, Tenant,
)
from app.services.policy import (
    PolicyError, activate_policy, compute_effective_window, evaluate_line,
)


def _contract(**kw):
    defaults = dict(
        tenant_id="t", contract_no="C1",
        valid_from=date(2026, 4, 1), valid_to=date(2027, 3, 31),
    )
    defaults.update(kw)
    return Contract(**defaults)


class TestClamp:
    def test_no_contract_passthrough(self):
        f, t, clamped, note = compute_effective_window(
            date(2026, 1, 1), date(2026, 12, 31), None
        )
        assert (f, t, clamped, note) == (date(2026, 1, 1), date(2026, 12, 31), False, None)

    def test_clamps_both_ends(self):
        f, t, clamped, note = compute_effective_window(
            date(2026, 1, 1), date(2027, 12, 31), _contract()
        )
        assert f == date(2026, 4, 1)
        assert t == date(2027, 3, 31)
        assert clamped
        assert "C1" in note

    def test_inside_contract_untouched(self):
        f, t, clamped, _ = compute_effective_window(
            date(2026, 6, 1), date(2026, 9, 30), _contract()
        )
        assert not clamped
        assert (f, t) == (date(2026, 6, 1), date(2026, 9, 30))

    def test_disjoint_window_raises(self):
        with pytest.raises(PolicyError):
            compute_effective_window(date(2028, 1, 1), date(2028, 6, 30), _contract())

    def test_inverted_window_raises(self):
        with pytest.raises(PolicyError):
            compute_effective_window(date(2026, 9, 1), date(2026, 6, 1), None)


class TestActivation:
    def _seed(self, db):
        db.add(Tenant(id="t", name="T"))
        contract = _contract()
        db.add(contract)
        db.flush()
        policy = Policy(
            tenant_id="t", code="P", name="P", version_no=1,
            status=PolicyStatus.DRAFT, contract_id=contract.id,
            requested_from=date(2026, 1, 1), requested_to=date(2027, 12, 31),
        )
        db.add(policy)
        db.flush()
        db.add(PolicyRule(
            tenant_id="t", policy_id=policy.id, code="R1",
            condition={"field": "line_total", "op": "gt", "value": 5000},
            action=PolicyAction.BLOCK,
        ))
        db.commit()
        return policy

    def test_activation_clamps_and_activates(self, db):
        policy = self._seed(db)
        activated = activate_policy(db, policy, actor="test")
        assert activated.status is PolicyStatus.ACTIVE
        assert activated.effective_from == date(2026, 4, 1)
        assert activated.effective_to == date(2027, 3, 31)
        assert activated.clamped

    def test_overlapping_prior_version_superseded(self, db):
        first = self._seed(db)
        activate_policy(db, first, actor="test")
        second = Policy(
            tenant_id="t", code="P", name="P", version_no=2,
            status=PolicyStatus.DRAFT, contract_id=first.contract_id,
            requested_from=date(2026, 6, 1), requested_to=date(2026, 12, 31),
        )
        db.add(second)
        db.flush()
        db.add(PolicyRule(
            tenant_id="t", policy_id=second.id, code="R1",
            condition={"field": "line_total", "op": "gt", "value": 1},
            action=PolicyAction.WARN,
        ))
        db.commit()
        activate_policy(db, second, actor="test")
        db.refresh(first)
        assert first.status is PolicyStatus.SUPERSEDED

    def test_no_rules_refused(self, db):
        db.add(Tenant(id="t", name="T"))
        policy = Policy(
            tenant_id="t", code="E", name="E", version_no=1,
            status=PolicyStatus.DRAFT,
            requested_from=date(2026, 1, 1), requested_to=date(2026, 12, 31),
        )
        db.add(policy)
        db.commit()
        with pytest.raises(PolicyError, match="no rules"):
            activate_policy(db, policy, actor="test")


class TestEvaluation:
    def _active_policy(self, db, rules):
        db.add(Tenant(id="t", name="T"))
        policy = Policy(
            tenant_id="t", code="P", name="P", version_no=1,
            status=PolicyStatus.ACTIVE,
            requested_from=date(2026, 1, 1), requested_to=date(2026, 12, 31),
            effective_from=date(2026, 1, 1), effective_to=date(2026, 12, 31),
        )
        db.add(policy)
        db.flush()
        for r in rules:
            db.add(PolicyRule(tenant_id="t", policy_id=policy.id, **r))
        db.commit()

    def test_most_severe_action_wins(self, db):
        self._active_policy(db, [
            dict(code="A", condition={"field": "line_total", "op": "gt", "value": 100},
                 action=PolicyAction.WARN),
            dict(code="B", condition={"field": "line_total", "op": "gt", "value": 100},
                 action=PolicyAction.BLOCK),
        ])
        result = evaluate_line(db, "t", {"line_total": 500}, on=date(2026, 6, 1))
        assert result.outcome is PolicyAction.BLOCK
        assert {f.rule_code for f in result.fired} == {"A", "B"}

    def test_scope_fails_closed_when_field_missing(self, db):
        self._active_policy(db, [
            dict(code="S", scope={"plants": ["2000"]},
                 condition={"field": "line_total", "op": "gt", "value": 0},
                 action=PolicyAction.BLOCK),
        ])
        # No plant in context -> rule out of scope -> allow.
        result = evaluate_line(db, "t", {"line_total": 500}, on=date(2026, 6, 1))
        assert result.outcome is PolicyAction.ALLOW

    def test_uncomparable_condition_is_nonmatch(self, db):
        self._active_policy(db, [
            dict(code="C", condition={"field": "line_total", "op": "gt", "value": 100},
                 action=PolicyAction.BLOCK),
        ])
        result = evaluate_line(db, "t", {"line_total": "not-a-number"}, on=date(2026, 6, 1))
        assert result.outcome is PolicyAction.ALLOW

    def test_expired_policy_ignored(self, db):
        self._active_policy(db, [
            dict(code="E", condition={"field": "line_total", "op": "gt", "value": 0},
                 action=PolicyAction.BLOCK),
        ])
        result = evaluate_line(db, "t", {"line_total": 500}, on=date(2027, 6, 1))
        assert result.outcome is PolicyAction.ALLOW

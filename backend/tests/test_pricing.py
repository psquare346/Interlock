from datetime import date
from decimal import Decimal

from app.models import (
    CatalogItem, CatalogVersion, Contract, ItemState, PriceTier, Supplier,
    Tenant, VersionStatus,
)
from app.services.pricing import (
    ParsedTier, apply_discount_ladder, detect_wide_columns,
    parse_encoded_string, resolve_price, validate_ladder,
)


def _ladder(*specs):
    return [ParsedTier(min_qty=lo, max_qty=hi, unit_price=Decimal(p)) for lo, hi, p in specs]


class TestValidateLadder:
    def test_valid_ladder(self):
        errors = validate_ladder(_ladder((1, 9, "42"), (10, 49, "38.5"), (50, None, "35")))
        assert errors == []

    def test_gap(self):
        errors = validate_ladder(_ladder((1, 9, "42"), (15, None, "35")))
        assert any("Gap or overlap" in e for e in errors)

    def test_overlap(self):
        errors = validate_ladder(_ladder((1, 10, "42"), (10, None, "35")))
        assert any("Gap or overlap" in e for e in errors)

    def test_increasing_price_flagged(self):
        errors = validate_ladder(_ladder((1, 9, "35"), (10, None, "42")))
        assert any("Price increases" in e for e in errors)

    def test_must_start_at_one(self):
        errors = validate_ladder(_ladder((5, 9, "42"), (10, None, "35")))
        assert any("expected 1" in e for e in errors)

    def test_exactly_one_open_tier(self):
        errors = validate_ladder(_ladder((1, 9, "42"), (10, 49, "38")))
        assert any("open-ended" in e for e in errors)

    def test_contract_currency_mismatch(self):
        contract = Contract(
            tenant_id="t", contract_no="C", currency="EUR",
            valid_from=date(2026, 1, 1), valid_to=date(2026, 12, 31),
        )
        errors = validate_ladder(
            _ladder((1, None, "42")), contract,
            valid_from=date(2026, 1, 1), valid_to=date(2026, 12, 31),
        )
        assert any("does not match" in e for e in errors)


class TestParsers:
    def test_encoded_string(self):
        result = parse_encoded_string("1-9:42.00;10-49:38.50;50+:35.00")
        assert result.ok
        assert [t.min_qty for t in result.tiers] == [1, 10, 50]
        assert result.tiers[-1].max_qty is None

    def test_wide_columns(self):
        found = detect_wide_columns(["sku", "price_1_9", "price_10_49", "price_50_plus"])
        assert [(lo, hi) for _, lo, hi in found] == [(1, 9), (10, 49), (50, None)]

    def test_discount_ladder_arithmetic(self):
        result = apply_discount_ladder(
            Decimal("100"), [(1, Decimal("0")), (10, Decimal("10")), (50, Decimal("20"))]
        )
        assert [t.unit_price for t in result.tiers] == [
            Decimal("100.0000"), Decimal("90.0000"), Decimal("80.0000"),
        ]
        assert validate_ladder(result.tiers) == []


class TestResolution:
    def _seed(self, db):
        db.add(Tenant(id="t", name="T"))
        supplier = Supplier(tenant_id="t", code="S", name="S")
        db.add(supplier)
        db.flush()
        version = CatalogVersion(
            tenant_id="t", supplier_id=supplier.id, status=VersionStatus.PUBLISHED
        )
        db.add(version)
        db.flush()
        item = CatalogItem(
            tenant_id="t", version_id=version.id, supplier_id=supplier.id,
            supplier_part_id="P1", description="Widget", unit_price=Decimal("50"),
            price_unit=1, currency="USD", state=ItemState.APPROVED,
        )
        db.add(item)
        db.flush()
        contract = Contract(
            tenant_id="t", supplier_id=supplier.id, contract_no="C",
            valid_from=date(2026, 1, 1), valid_to=date(2026, 12, 31),
        )
        db.add(contract)
        db.flush()
        for lo, hi, price in ((1, 9, "42"), (10, 49, "38.5"), (50, None, "35")):
            db.add(PriceTier(
                tenant_id="t", item_id=item.id, contract_id=contract.id,
                min_qty=lo, max_qty=hi, unit_price=Decimal(price),
                price_unit=100, currency="USD",
                valid_from=date(2026, 1, 1), valid_to=date(2026, 12, 31),
            ))
        db.commit()
        return item

    def test_tier_hit_with_price_unit(self, db):
        item = self._seed(db)
        r = resolve_price(db, item, 50, on=date(2026, 6, 1))
        assert r.source == "tier"
        assert r.unit_price == Decimal("35")
        # The per-100 trap: effective price is unit/price_unit.
        assert r.effective_unit_price == Decimal("0.35")
        assert r.extended == Decimal("17.50")

    def test_next_break_surfaced(self, db):
        item = self._seed(db)
        r = resolve_price(db, item, 5, on=date(2026, 6, 1))
        assert r.next_break == {
            "qty": 10, "unit_price": 0.385,
            "saving_per_unit": float(Decimal("0.42") - Decimal("0.385")),
        }

    def test_expired_tiers_fall_back_to_list(self, db):
        item = self._seed(db)
        r = resolve_price(db, item, 10, on=date(2027, 6, 1))
        assert r.source == "list"
        assert r.unit_price == Decimal("50")

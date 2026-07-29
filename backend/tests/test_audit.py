"""Leakage audit: the header mapper, the math, and the detail report."""

from datetime import date
from decimal import Decimal

import pytest

from app.models import (
    CatalogItem, CatalogVersion, Contract, ItemState, PriceTier, Supplier,
    Tenant, VersionStatus,
)
from app.services import storage
from app.services.audit import AuditError, _map_headers, run_leakage_audit


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "STORAGE_DIR", str(tmp_path))


def _seed(db):
    """Published item P1: contract price 0.42 (qty 1-9), 0.385 (10-49), 0.35 (50+)."""
    db.add(Tenant(id="t", name="T"))
    supplier = Supplier(tenant_id="t", code="S", name="S")
    db.add(supplier)
    db.flush()
    version = CatalogVersion(tenant_id="t", supplier_id=supplier.id,
                             status=VersionStatus.PUBLISHED)
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


class TestHeaderMapping:
    def test_sap_style_headers(self):
        mapping = _map_headers(["EBELN", "BEDAT", "IDNLF", "MENGE", "NETPR"])
        assert mapping == {
            "po_number": "EBELN", "date": "BEDAT", "part": "IDNLF",
            "quantity": "MENGE", "unit_price": "NETPR",
        }

    def test_friendly_headers(self):
        mapping = _map_headers(["PO Number", "Order Date", "Part", "Qty", "Price Paid"])
        assert mapping["po_number"] == "PO Number"
        assert mapping["unit_price"] == "Price Paid"

    def test_missing_columns_named(self):
        with pytest.raises(AuditError, match="unit_price"):
            _map_headers(["po_number", "part", "quantity"])


class TestAudit:
    CSV = (
        "po_number,date,part,quantity,unit_price\n"
        "PO-1,2026-06-01,P1,50,0.35\n"    # exactly contract → no leakage
        "PO-2,2026-06-01,P1,10,0.50\n"    # contract 0.385 → leak 1.15
        "PO-3,2026-06-01,P1,5,0.40\n"     # contract 0.42 → UNDERpaid 0.10
        "PO-4,2026-06-01,NOPE,3,9.99\n"   # off catalog
        "PO-5,2026-06-01,P1,junk,0.42\n"  # unparseable qty
    )

    def _run(self, db):
        key = storage.put("t/audit-uploads/x.csv", self.CSV.encode())
        return run_leakage_audit(db, {
            "storage_key": key, "tenant_id": "t", "filename": "x.csv", "job_id": "j1",
        })

    def test_summary_math(self, db):
        _seed(db)
        result = self._run(db)
        assert result["lines"] == 5
        assert result["matched_lines"] == 3
        assert result["off_catalog_lines"] == 1
        assert result["unparseable_lines"] == 1
        assert result["leakage_total"] == pytest.approx(1.15)
        assert result["underpaid_total"] == pytest.approx(0.10)
        assert result["top_offenders"] == [{"part": "P1", "leakage": 1.15}]

    def test_detail_report_written(self, db):
        _seed(db)
        result = self._run(db)
        report = storage.get(result["report_key"]).decode()
        lines = report.strip().splitlines()
        assert lines[0].startswith("po_number,part,date")
        assert len(lines) == 5  # header + 4 data rows (unparseable line skipped)
        assert any("overpaid" in l for l in lines)
        assert any("off_catalog" in l for l in lines)

    def test_semicolon_delimiter(self, db):
        _seed(db)
        csv_semicolon = "po_number;part;quantity;unit_price\nPO-1;P1;10;0.50\n"
        key = storage.put("t/audit-uploads/y.csv", csv_semicolon.encode())
        result = run_leakage_audit(db, {
            "storage_key": key, "tenant_id": "t", "job_id": "j2",
        })
        assert result["matched_lines"] == 1
        assert result["leakage_total"] == pytest.approx(1.15)

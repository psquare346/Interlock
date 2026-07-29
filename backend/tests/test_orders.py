"""PO receipt: parsing, idempotency, price-truth verdicts, lifecycle."""

from datetime import date
from decimal import Decimal

import pytest

from app.models import (
    CatalogItem, CatalogVersion, Contract, ItemState, OrderEvent, OrderLine,
    OrderStatus, PriceTier, PriceVerdict, Supplier, Tenant, VersionStatus,
)
from app.services.orders import (
    PoError, parse_po_idoc, parse_po_json, receive_po, transition,
)

IDOC_XML = b"""<?xml version="1.0"?>
<ORDERS05>
  <IDOC>
    <E1EDK01><BELNR>4500001234</BELNR><CURCY>USD</CURCY></E1EDK01>
    <E1EDK03><IDDAT>012</IDDAT><DATUM>20260601</DATUM></E1EDK03>
    <E1EDKA1><PARVW>LF</PARVW><PARTN>0000100001</PARTN></E1EDKA1>
    <E1EDP01>
      <POSEX>00010</POSEX><MENGE>50</MENGE><MENEE>EA</MENEE><NETPR>0.35</NETPR>
      <E1EDP19><QUALF>002</QUALF><IDTNR>P1</IDTNR><KTEXT>Widget</KTEXT></E1EDP19>
    </E1EDP01>
    <E1EDP01>
      <POSEX>00020</POSEX><MENGE>5</MENGE><MENEE>EA</MENEE><NETPR>9.99</NETPR>
      <E1EDP19><QUALF>002</QUALF><IDTNR>UNKNOWN-PART</IDTNR><KTEXT>Mystery</KTEXT></E1EDP19>
    </E1EDP01>
  </IDOC>
</ORDERS05>"""


def _seed(db):
    """Tenant t, supplier S (vendor no 0000100001), published item P1 with a
    contract tier: 50+ @ 35/100 = 0.35 effective (same seed as pricing tests)."""
    db.add(Tenant(id="t", name="T"))
    supplier = Supplier(tenant_id="t", code="S", name="S", sap_vendor_no="0000100001")
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
    return supplier


class TestParsing:
    def test_json_happy_path(self):
        parsed = parse_po_json({
            "po_number": "4500001234", "currency": "usd",  # 3-char slice
            "ordered_at": "2026-06-01",
            "supplier": {"code": "S"},
            "lines": [{"part": "P1", "quantity": 50, "unit_price": 0.35}],
        })
        assert parsed["po_number"] == "4500001234"
        assert parsed["ordered_at"] == date(2026, 6, 1)
        assert parsed["lines"][0]["line_no"] == 1

    def test_json_rejects_missing_fields(self):
        with pytest.raises(PoError):
            parse_po_json({"lines": [{"part": "P1", "unit_price": 1}]})
        with pytest.raises(PoError):
            parse_po_json({"po_number": "X", "lines": []})
        with pytest.raises(PoError):
            parse_po_json({"po_number": "X", "lines": [{"part": "P1"}]})

    def test_idoc_xml(self):
        parsed = parse_po_idoc(IDOC_XML)
        assert parsed["po_number"] == "4500001234"
        assert parsed["sap_vendor_no"] == "0000100001"
        assert parsed["ordered_at"] == date(2026, 6, 1)
        assert len(parsed["lines"]) == 2
        assert parsed["lines"][0] == {
            "line_no": 10, "part": "P1", "description": "Widget",
            "quantity": 50.0, "uom": "EA", "unit_price": 0.35, "currency": None,
        }

    def test_idoc_rejects_garbage(self):
        with pytest.raises(PoError):
            parse_po_idoc(b"not xml at all")
        with pytest.raises(PoError):
            parse_po_idoc(b"<ORDERS05><IDOC></IDOC></ORDERS05>")


class TestReceipt:
    def test_receive_matches_supplier_and_verifies_prices(self, db):
        _seed(db)
        parsed = parse_po_idoc(IDOC_XML)
        order, created = receive_po(db, "t", parsed, source="idoc")

        assert created
        assert order.status is OrderStatus.RECEIVED
        assert order.supplier_id is not None
        lines = db.query(OrderLine).filter_by(order_id=order.id).order_by(OrderLine.line_no).all()

        # Line 10: paid exactly the contracted 0.35 → match, contract linked.
        assert lines[0].price_verdict is PriceVerdict.MATCH
        assert lines[0].contract_id is not None
        # Line 20: part not in the catalog → off_catalog, no expected price.
        assert lines[1].price_verdict is PriceVerdict.OFF_CATALOG
        assert lines[1].expected_unit_price is None

    def test_overpaid_line_flagged_with_delta(self, db):
        _seed(db)
        order, _ = receive_po(db, "t", parse_po_json({
            "po_number": "PO-1", "ordered_at": "2026-06-01",
            "supplier": {"code": "S"},
            # Contract says 0.385 at qty 10; paying 0.50 → overpaid by 0.115*10.
            "lines": [{"part": "P1", "quantity": 10, "unit_price": 0.50}],
        }), source="json")
        line = db.query(OrderLine).filter_by(order_id=order.id).one()
        assert line.price_verdict is PriceVerdict.OVERPAID
        assert float(line.expected_unit_price) == 0.385
        assert float(line.price_delta) == pytest.approx(1.15)

    def test_idempotent_re_push_updates_in_place(self, db):
        _seed(db)
        parsed = parse_po_idoc(IDOC_XML)
        first, created1 = receive_po(db, "t", parsed, source="idoc")
        assert created1

        # SAP re-pushes the same PO with one line changed.
        parsed["lines"] = parsed["lines"][:1]
        second, created2 = receive_po(db, "t", parsed, source="idoc")

        assert not created2
        assert second.id == first.id
        assert second.sap_po_version == 2
        assert db.query(OrderLine).filter_by(order_id=first.id).count() == 1
        types = [e.type for e in db.query(OrderEvent).filter_by(order_id=first.id)
                 .order_by(OrderEvent.created_at).all()]
        assert types == ["received", "updated"]

    def test_unknown_supplier_still_lands(self, db):
        db.add(Tenant(id="t", name="T"))
        db.commit()
        order, _ = receive_po(db, "t", parse_po_json({
            "po_number": "PO-2",
            "lines": [{"part": "X", "quantity": 1, "unit_price": 5}],
        }), source="json")
        assert order.supplier_id is None  # visible, flagged, not lost


class TestLifecycle:
    def _order(self, db):
        db.add(Tenant(id="t", name="T"))
        db.commit()
        order, _ = receive_po(db, "t", parse_po_json({
            "po_number": "PO-3",
            "lines": [{"part": "X", "quantity": 1, "unit_price": 5}],
        }), source="json")
        return order

    def test_happy_path(self, db):
        order = self._order(db)
        transition(db, order, OrderStatus.ACKNOWLEDGED, actor="v@x.com")
        transition(db, order, OrderStatus.SHIPPED, actor="v@x.com",
                   data={"tracking_number": "1Z999"})
        transition(db, order, OrderStatus.DELIVERED, actor="buyer@t.com")
        types = [e.type for e in db.query(OrderEvent).filter_by(order_id=order.id)
                 .order_by(OrderEvent.created_at).all()]
        assert types == ["received", "acknowledged", "shipped", "delivered"]

    def test_illegal_transition_refused(self, db):
        order = self._order(db)
        with pytest.raises(PoError):
            transition(db, order, OrderStatus.DELIVERED, actor="x")  # skip shipped
        transition(db, order, OrderStatus.SHIPPED, actor="v")  # received→shipped ok
        with pytest.raises(PoError):
            transition(db, order, OrderStatus.ACKNOWLEDGED, actor="v")  # backwards

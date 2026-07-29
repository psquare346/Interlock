from decimal import Decimal

from app.models import ItemState, Supplier, Tenant
from app.services.ingestion import (
    _parse_decimal, ingest, lookup_uom, map_columns, parse_file,
)

GERMAN_CSV = (
    "Art.-Nr.;Bezeichnung;ME;Preis;VPE;Währung\n"
    "A-1;Schraube M8;ST;42,00;100;USD\n"
    "A-2;Mutter M8;Stk;1.234,56;1;USD\n"
    ";Kaputte Zeile;ST;10,00;1;USD\n"
    "A-3;Kartusche;KRT;5,00;1;USD\n"
).encode("utf-8")


class TestParsing:
    def test_semicolon_and_umlauts(self):
        headers, rows = parse_file("x.csv", GERMAN_CSV)
        assert headers[0] == "Art.-Nr."
        assert len(rows) == 4

    def test_decimal_shapes(self):
        assert _parse_decimal("42,00") == Decimal("42.00")
        assert _parse_decimal("1.234,56") == Decimal("1234.56")
        assert _parse_decimal("1,234.56") == Decimal("1234.56")
        assert _parse_decimal("$1,234.56") == Decimal("1234.56")
        assert _parse_decimal("junk") is None

    def test_uom_lookup(self):
        assert lookup_uom("ST") == "EA"
        assert lookup_uom("Stk") == "EA"
        assert lookup_uom("karton") == "KAR"
        assert lookup_uom("PAAR") == "PAA"
        assert lookup_uom("KRT") is None


class TestMapping:
    def test_german_headers_map_by_rules(self):
        headers, rows = parse_file("x.csv", GERMAN_CSV)
        result = map_columns(headers, rows)
        assert result.source == "rules"
        assert result.missing_required == []
        assert result.column_for("supplier_part_id") == "Art.-Nr."
        assert result.column_for("currency") == "Währung"

    def test_confirmed_mapping_skips_everything(self):
        confirmed = {"mappings": [
            {"source_column": "A", "target_field": t, "confidence": 1.0}
            for t in ("supplier_part_id", "description", "uom", "unit_price", "currency")
        ]}
        result = map_columns(["A"], [], confirmed)
        assert result.source == "confirmed"
        assert result.missing_required == []


class TestIngest:
    def _supplier(self, db):
        db.add(Tenant(id="t", name="T"))
        supplier = Supplier(tenant_id="t", code="S", name="S")
        db.add(supplier)
        db.commit()
        return supplier

    def test_row_states(self, db):
        supplier = self._supplier(db)
        version = ingest(db, "t", supplier, "cat.csv", GERMAN_CSV)
        by_part = {i.supplier_part_id: i for i in version.items}

        # Missing part id is manual, always — never invented.
        broken = next(i for i in version.items if i.supplier_part_id is None)
        assert broken.state is ItemState.MANUAL

        # Unknown unit is a hard fail.
        assert by_part["A-3"].state is ItemState.MANUAL
        assert any("T006" in r for r in by_part["A-3"].review_reasons)

        # Normalized rows parse German decimals and land in review (no AI key).
        assert by_part["A-1"].uom_sap == "EA"
        assert Decimal(str(by_part["A-1"].unit_price)) == Decimal("42")
        assert Decimal(str(by_part["A-2"].unit_price)) == Decimal("1234.56")
        assert by_part["A-1"].state is ItemState.NEEDS_REVIEW

    def test_duplicate_part_id_hard_fails(self, db):
        supplier = self._supplier(db)
        dup = (
            "Art.-Nr.;Bezeichnung;ME;Preis;Währung\n"
            "A-1;Erste;ST;10,00;USD\n"
            "A-1;Zweite;ST;12,00;USD\n"
        ).encode("utf-8")
        version = ingest(db, "t", supplier, "dup.csv", dup)
        states = [i.state for i in version.items]
        assert ItemState.MANUAL in states  # the second A-1

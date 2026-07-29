"""The simulated SAP receiver renders posted OCI fields as requisition lines."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

FIELDS = {
    "NEW_ITEM-DESCRIPTION[1]": "Hex bolt M8x40",
    "NEW_ITEM-QUANTITY[1]": "100",
    "NEW_ITEM-UNIT[1]": "EA",
    "NEW_ITEM-PRICE[1]": "0.3500",
    "NEW_ITEM-PRICEUNIT[1]": "1",
    "NEW_ITEM-CURRENCY[1]": "USD",
    "NEW_ITEM-VENDORMAT[1]": "ACME-1001",
    "NEW_ITEM-DESCRIPTION[2]": "Safety glasses",
    "NEW_ITEM-QUANTITY[2]": "5",
    "NEW_ITEM-UNIT[2]": "EA",
    "NEW_ITEM-PRICE[2]": "6.2000",
    "NEW_ITEM-VENDORMAT[2]": "ACME-1002",
    "SOME_OTHER_FIELD": "carried through",
}


def test_renders_lines_and_labels_itself_a_simulation():
    r = client.post("/api/punchout/oci/mock-requisition", data=FIELDS)
    assert r.status_code == 200
    body = r.text
    # Both lines rendered, with SAP-style item numbers.
    assert "ACME-1001" in body and "ACME-1002" in body
    assert "0010" in body and "0020" in body
    assert "2 line(s)" in body
    # Non-NEW_ITEM fields are surfaced, not silently dropped.
    assert "SOME_OTHER_FIELD" in body and "carried through" in body
    # Never claims to be SAP.
    assert "Simulated SAP receiver" in body


def test_escapes_html_in_values():
    r = client.post("/api/punchout/oci/mock-requisition", data={
        "NEW_ITEM-DESCRIPTION[1]": "<script>alert(1)</script>",
        "NEW_ITEM-VENDORMAT[1]": "X&Y",
    })
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text
    assert "X&amp;Y" in r.text


def test_empty_post_is_not_an_error():
    r = client.post("/api/punchout/oci/mock-requisition", data={})
    assert r.status_code == 200
    assert "No NEW_ITEM fields" in r.text


def test_json_mock_hook_still_works():
    r = client.post("/api/punchout/oci/mock-hook", data=FIELDS)
    assert r.status_code == 200
    assert r.json()["received_fields"]["NEW_ITEM-VENDORMAT[1]"] == "ACME-1001"

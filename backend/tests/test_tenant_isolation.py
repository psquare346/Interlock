"""Two customers on one platform: prove the walls hold.

Provisions two tenants end-to-end through the real HTTP surface — supplier,
catalog upload, publish, credentialed punchout, cart, OCI return — then
attacks every boundary: wrong secret, swapped secrets, missing secret,
cross-tenant sessions, cross-tenant admin tokens, and the storefront items
endpoint without any proof of tenancy.
"""

import io

from tests.conftest import TEST_OPERATOR_KEY

OP = {"X-Operator-Key": TEST_OPERATOR_KEY}

CSV_A = (
    "supplier_part_id,description,uom,unit_price,currency,price_unit,unspsc\n"
    "ACME-100,Acme copy paper carton,EA,30.00,USD,1,14111507\n"
    "ACME-200,Acme toner black,EA,80.00,USD,1,44103105\n"
)
CSV_B = (
    "supplier_part_id,description,uom,unit_price,currency,price_unit,unspsc\n"
    "GLX-900,Globex fusion widget,EA,120.00,USD,1,31201503\n"
)

HOOK = "http://sap.example.test/hook"


def build_tenant(client, tenant_id, name, email, supplier_code, csv_text):
    """Provision → register admin → supplier → upload → (approve) → publish.
    Returns dict with the tenant's credentials and admin auth header."""
    d = client.post("/api/ops/tenants", headers=OP, json={
        "tenant_id": tenant_id, "name": name, "admin_email": email,
    }).json()
    reg = client.post("/api/auth/register", json={
        "invite_token": d["admin_invite"]["invite_token"],
        "display_name": "Admin", "password": "hunter22!!",
    })
    assert reg.status_code == 201, reg.text
    hdr = {"Authorization": "Bearer " + reg.json()["token"]}

    r = client.post("/api/suppliers", headers=hdr, json={
        "code": supplier_code, "name": f"{supplier_code} Inc"})
    assert r.status_code == 201, r.text

    up = client.post(
        "/api/catalog/upload", headers=hdr,
        data={"supplier_code": supplier_code},
        files={"file": ("catalog.csv", io.BytesIO(csv_text.encode()), "text/csv")},
    )
    assert up.status_code == 201, up.text
    version_id = up.json()["version_id"]

    # Approve anything the triage queued so publish can't refuse.
    review = client.get(f"/api/catalog/versions/{version_id}/review",
                        headers=hdr).json()
    for item in review["items"]:
        if item["state"] in ("needs_review", "manual"):
            dec = client.post(f"/api/catalog/items/{item['id']}/decide",
                              params={"decision": "approve"}, headers=hdr)
            assert dec.status_code == 200, dec.text

    pub = client.post(f"/api/catalog/versions/{version_id}/publish", headers=hdr)
    assert pub.status_code == 200, pub.text
    assert pub.json()["published_items"] > 0

    return {
        "tenant_id": tenant_id,
        "secret": d["punchout_secret"],
        "po_key": d["po_key"],
        "hdr": hdr,
    }


def start_session(client, tenant_id, secret, **extra):
    return client.get("/api/punchout/oci/start", params={
        "tenant_id": tenant_id, "PASSWORD": secret, "HOOK_URL": HOOK, **extra,
    })


def two_tenants(client):
    a = build_tenant(client, "acme", "Acme Corp", "admin@acme.com", "ACME", CSV_A)
    b = build_tenant(client, "globex", "Globex", "admin@globex.com", "GLX", CSV_B)
    return a, b


class TestFrontDoor:
    def test_right_secret_opens_a_session(self, client):
        a, _ = two_tenants(client)
        r = start_session(client, "acme", a["secret"])
        assert r.status_code == 200, r.text
        assert r.json()["session_id"]

    def test_wrong_missing_and_swapped_secrets_all_401(self, client):
        a, b = two_tenants(client)
        assert start_session(client, "acme", "wrong-secret").status_code == 401
        assert client.get("/api/punchout/oci/start", params={
            "tenant_id": "acme", "HOOK_URL": HOOK}).status_code == 401
        # Globex's real secret must not open Acme's shop, or vice versa.
        assert start_session(client, "acme", b["secret"]).status_code == 401
        assert start_session(client, "globex", a["secret"]).status_code == 401

    def test_unknown_tenant_gets_same_401(self, client):
        a, _ = two_tenants(client)
        r_unknown = start_session(client, "nosuch", a["secret"])
        r_wrong = start_session(client, "acme", "wrong")
        assert r_unknown.status_code == r_wrong.status_code == 401
        # Same body → no tenant-existence oracle.
        assert r_unknown.json() == r_wrong.json()

    def test_tenant_without_secret_is_closed(self, client):
        # Legacy tenant shape: exists but never got a punchout secret.
        from app.db import get_db
        from app.main import app
        from app.models import Tenant
        gen = app.dependency_overrides[get_db]()
        db = next(gen)
        db.add(Tenant(id="legacy", name="Legacy Co"))
        db.commit()
        assert start_session(client, "legacy", "").status_code == 401
        assert start_session(client, "legacy", "anything").status_code == 401

    def test_staff_bearer_opens_own_tenant_only(self, client):
        a, b = two_tenants(client)
        # Admin console demo loop: own tenant, no secret needed.
        r = client.get("/api/punchout/oci/start",
                       params={"tenant_id": "acme", "HOOK_URL": HOOK},
                       headers=a["hdr"])
        assert r.status_code == 200
        # Same token pointed at the other tenant: rejected.
        r2 = client.get("/api/punchout/oci/start",
                        params={"tenant_id": "globex", "HOOK_URL": HOOK},
                        headers=a["hdr"])
        assert r2.status_code == 401

    def test_secret_rotation_invalidates_old(self, client):
        a, _ = two_tenants(client)
        rot = client.post("/api/tenants/punchout-secret", headers=a["hdr"])
        assert rot.status_code == 200
        new_secret = rot.json()["punchout_secret"]
        assert start_session(client, "acme", a["secret"]).status_code == 401
        assert start_session(client, "acme", new_secret).status_code == 200


class TestStorefrontIsolation:
    def test_session_sees_only_its_tenants_items(self, client):
        a, b = two_tenants(client)
        sess_a = start_session(client, "acme", a["secret"]).json()["session_id"]
        sess_b = start_session(client, "globex", b["secret"]).json()["session_id"]

        items_a = client.get("/api/catalog/items",
                             params={"session": sess_a}).json()
        items_b = client.get("/api/catalog/items",
                             params={"session": sess_b}).json()
        parts_a = {i["supplier_part_id"] for i in items_a}
        parts_b = {i["supplier_part_id"] for i in items_b}
        assert parts_a == {"ACME-100", "ACME-200"}
        assert parts_b == {"GLX-900"}

    def test_items_need_proof_of_tenancy(self, client):
        two_tenants(client)
        # Bare request, bogus session, and the OLD tenant_id-only shape: all 401.
        assert client.get("/api/catalog/items").status_code == 401
        assert client.get("/api/catalog/items",
                          params={"session": "f" * 32}).status_code == 401
        assert client.get("/api/catalog/items",
                          params={"tenant_id": "acme"}).status_code == 401

    def test_closed_session_stops_serving_items(self, client):
        a, _ = two_tenants(client)
        sess = start_session(client, "acme", a["secret"]).json()["session_id"]
        client.post(f"/api/punchout/sessions/{sess}/cart/add",
                    json={"part": "ACME-100", "quantity": 2})
        ret = client.post(f"/api/punchout/sessions/{sess}/return",
                          params={"format": "json"})
        assert ret.status_code == 200
        assert client.get("/api/catalog/items",
                          params={"session": sess}).status_code == 401

    def test_staff_token_sees_own_catalog(self, client):
        a, _ = two_tenants(client)
        items = client.get("/api/catalog/items", headers=a["hdr"]).json()
        assert {i["supplier_part_id"] for i in items} == {"ACME-100", "ACME-200"}

    def test_cart_rejects_parts_from_other_tenant(self, client):
        a, b = two_tenants(client)
        sess_a = start_session(client, "acme", a["secret"]).json()["session_id"]
        r = client.post(f"/api/punchout/sessions/{sess_a}/cart/add",
                        json={"part": "GLX-900", "quantity": 1})
        assert r.status_code == 404  # Globex's part does not exist for Acme


class TestRoundTrip:
    def test_full_oci_round_trip_carries_only_own_lines(self, client):
        a, _ = two_tenants(client)
        sess = start_session(client, "acme", a["secret"],
                             USERNAME="REQ-USER-1").json()["session_id"]
        add = client.post(f"/api/punchout/sessions/{sess}/cart/add",
                          json={"part": "ACME-100", "quantity": 10})
        assert add.status_code == 200, add.text
        ret = client.post(f"/api/punchout/sessions/{sess}/return",
                          params={"format": "json"})
        assert ret.status_code == 200
        body = ret.json()
        assert body["hook_url"] == HOOK
        assert body["fields"]["NEW_ITEM-VENDORMAT[1]"] == "ACME-100"
        assert body["fields"]["NEW_ITEM-QUANTITY[1]"] == "10"
        assert "NEW_ITEM-VENDORMAT[2]" not in body["fields"]

    def test_browser_flow_redirects_to_shop_without_tenant_leak(self, client):
        a, _ = two_tenants(client)
        r = client.get("/api/punchout/oci/start", params={
            "tenant_id": "acme", "PASSWORD": a["secret"], "HOOK_URL": HOOK,
        }, headers={"Accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 302
        loc = r.headers["location"]
        assert loc.startswith("/shop?session=")
        assert "tenant_id" not in loc and a["secret"] not in loc


class TestAdminIsolation:
    def test_admin_apis_are_tenant_scoped(self, client):
        a, b = two_tenants(client)
        users_a = client.get("/api/auth/users", headers=a["hdr"]).json()
        assert {u["tenant_id"] for u in users_a} == {"acme"}
        suppliers_b = client.get("/api/suppliers", headers=b["hdr"]).json()
        assert {s["code"] for s in suppliers_b} == {"GLX"}
        versions_a = client.get("/api/catalog/versions", headers=a["hdr"]).json()
        assert all(v["supplier_code"] == "ACME" for v in versions_a)

    def test_ops_endpoints_reject_tenant_admin_tokens(self, client):
        a, _ = two_tenants(client)
        r = client.get("/api/ops/tenants", headers=a["hdr"])
        assert r.status_code == 401  # bearer token is not an operator key

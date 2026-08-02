"""Regression tests for the issues the adversarial validation workflow found.

Each class maps to one confirmed finding; the test fails against the pre-fix
code and passes after. Reuses the two-tenant harness from test_tenant_isolation.
"""

from datetime import datetime, timedelta, timezone

from tests.conftest import TEST_OPERATOR_KEY
from tests.test_tenant_isolation import (
    OP, build_tenant, start_session, two_tenants, HOOK, CSV_A,
)


class TestPriceResolveIsScoped:
    """Finding: /api/pricing/resolve was unauthenticated and trusted tenant_id,
    leaking any tenant's negotiated prices."""

    def test_unauthenticated_resolve_is_401(self, client):
        two_tenants(client)
        r = client.get("/api/pricing/resolve",
                       params={"part": "ACME-100", "quantity": 10, "tenant_id": "acme"})
        assert r.status_code == 401

    def test_resolve_derives_tenant_from_login_not_param(self, client):
        a, b = two_tenants(client)
        # Globex admin asks for an Acme part while spoofing tenant_id=acme.
        r = client.get("/api/pricing/resolve", headers=b["hdr"],
                       params={"part": "ACME-100", "quantity": 10, "tenant_id": "acme"})
        assert r.status_code == 404  # not found in Globex; no Acme price leaks

    def test_resolve_works_for_own_tenant(self, client):
        a, _ = two_tenants(client)
        r = client.get("/api/pricing/resolve", headers=a["hdr"],
                       params={"part": "ACME-100", "quantity": 10})
        assert r.status_code == 200
        assert r.json()["unit_price"] is not None


class TestCartPublishedOnly:
    """Finding: cart/add priced parts regardless of publish/approval state."""

    def _rejected_part_setup(self, client):
        import io
        d = client.post("/api/ops/tenants", headers=OP, json={
            "tenant_id": "acme", "name": "Acme", "admin_email": "a@acme.com"}).json()
        hdr = {"Authorization": "Bearer " + client.post("/api/auth/register", json={
            "invite_token": d["admin_invite"]["invite_token"],
            "display_name": "A", "password": "hunter22!!"}).json()["token"]}
        client.post("/api/suppliers", headers=hdr, json={"code": "ACME", "name": "Acme"})
        # Two parts; we will REJECT one and leave the version unpublished-then-publish.
        csv = ("supplier_part_id,description,uom,unit_price,currency,price_unit,unspsc\n"
               "GOOD-1,Published good,EA,10.00,USD,1,14111507\n"
               "SECRET-9,Withheld part,EA,99.00,USD,1,14111507\n")
        up = client.post("/api/catalog/upload", headers=hdr,
                         data={"supplier_code": "ACME"},
                         files={"file": ("c.csv", io.BytesIO(csv.encode()), "text/csv")})
        vid = up.json()["version_id"]
        review = client.get(f"/api/catalog/versions/{vid}/review", headers=hdr).json()
        for item in review["items"]:
            decision = "reject" if item["supplier_part_id"] == "SECRET-9" else "approve"
            client.post(f"/api/catalog/items/{item['id']}/decide",
                        params={"decision": decision}, headers=hdr)
        client.post(f"/api/catalog/versions/{vid}/publish", headers=hdr)
        d2 = client.post("/api/ops/tenants/acme/punchout-secret", headers=OP).json()
        return d2["punchout_secret"]

    def test_rejected_part_cannot_be_carted(self, client):
        secret = self._rejected_part_setup(client)
        sess = start_session(client, "acme", secret).json()["session_id"]
        # The published part works...
        ok = client.post(f"/api/punchout/sessions/{sess}/cart/add",
                         json={"part": "GOOD-1", "quantity": 1})
        assert ok.status_code == 200
        # ...the rejected one, known by exact id, does NOT.
        bad = client.post(f"/api/punchout/sessions/{sess}/cart/add",
                          json={"part": "SECRET-9", "quantity": 1})
        assert bad.status_code == 404
        # And it never appears in the storefront listing either.
        items = client.get("/api/catalog/items", params={"session": sess}).json()
        assert {i["supplier_part_id"] for i in items} == {"GOOD-1"}


class TestPrivilegeEscalation:
    """Finding: a member with manage_users could mint admins or self-promote."""

    def _admin_and_delegate(self, client):
        """Return (admin_hdr, member_hdr) where member has ONLY manage_users."""
        a = build_tenant(client, "acme", "Acme", "admin@acme.com", "ACME", CSV_A)
        inv = client.post("/api/auth/invites", headers=a["hdr"], json={
            "email": "mgr@acme.com", "role": "member",
            "privileges": ["manage_users"]}).json()
        mtoken = client.post("/api/auth/register", json={
            "invite_token": inv["invite_token"], "display_name": "Mgr",
            "password": "hunter22!!"}).json()["token"]
        return a["hdr"], {"Authorization": "Bearer " + mtoken}

    def test_member_cannot_invite_admin(self, client):
        _, mhdr = self._admin_and_delegate(client)
        r = client.post("/api/auth/invites", headers=mhdr, json={
            "email": "x@acme.com", "role": "admin"})
        assert r.status_code == 403

    def test_member_cannot_grant_privilege_it_lacks(self, client):
        _, mhdr = self._admin_and_delegate(client)
        r = client.post("/api/auth/invites", headers=mhdr, json={
            "email": "x@acme.com", "role": "member",
            "privileges": ["catalog_publish"]})  # member doesn't hold this
        assert r.status_code == 403

    def test_member_can_grant_privilege_it_holds(self, client):
        _, mhdr = self._admin_and_delegate(client)
        r = client.post("/api/auth/invites", headers=mhdr, json={
            "email": "x@acme.com", "role": "member",
            "privileges": ["manage_users"]})
        assert r.status_code == 201

    def test_member_cannot_self_promote_to_admin(self, client):
        a, mhdr = self._admin_and_delegate(client)
        me = client.get("/api/auth/me", headers=mhdr).json()
        r = client.patch(f"/api/auth/users/{me['id']}", headers=mhdr,
                         json={"role": "admin"})
        assert r.status_code == 403

    def test_member_cannot_grant_others_privileges_beyond_own(self, client):
        ahdr, mhdr = self._admin_and_delegate(client)
        # Create a plain member for the delegate to edit.
        inv = client.post("/api/auth/invites", headers=ahdr, json={
            "email": "victim@acme.com", "role": "member", "privileges": []}).json()
        vtoken = client.post("/api/auth/register", json={
            "invite_token": inv["invite_token"], "display_name": "V",
            "password": "hunter22!!"}).json()
        vid = client.get("/api/auth/me",
                         headers={"Authorization": "Bearer " + vtoken["token"]}).json()["id"]
        r = client.patch(f"/api/auth/users/{vid}", headers=mhdr,
                         json={"privileges": ["pricing_manage"]})
        assert r.status_code == 403

    def test_admin_still_can_do_everything(self, client):
        ahdr, _ = self._admin_and_delegate(client)
        r = client.post("/api/auth/invites", headers=ahdr, json={
            "email": "newadmin@acme.com", "role": "admin"})
        assert r.status_code == 201


class TestSessionTTL:
    """Finding: open sessions never expired, serving the catalog forever."""

    def _backdate_session(self, client, session_id, hours):
        from app.db import get_db
        from app.main import app
        from app.models import PunchoutSession
        db = next(app.dependency_overrides[get_db]())
        ps = db.get(PunchoutSession, session_id)
        ps.created_at = datetime.now(timezone.utc) - timedelta(hours=hours)
        db.commit()

    def test_stale_session_stops_serving_items(self, client):
        a, _ = two_tenants(client)
        sess = start_session(client, "acme", a["secret"]).json()["session_id"]
        assert client.get("/api/catalog/items",
                          params={"session": sess}).status_code == 200
        self._backdate_session(client, sess, hours=13)  # past the 12h TTL
        assert client.get("/api/catalog/items",
                          params={"session": sess}).status_code == 401

    def test_stale_session_cannot_add_to_cart(self, client):
        a, _ = two_tenants(client)
        sess = start_session(client, "acme", a["secret"]).json()["session_id"]
        self._backdate_session(client, sess, hours=13)
        r = client.post(f"/api/punchout/sessions/{sess}/cart/add",
                        json={"part": "ACME-100", "quantity": 1})
        assert r.status_code == 409

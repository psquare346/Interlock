"""Operator provisioning + invite-only registration.

The contract under test: tenants exist only when the operator creates them,
accounts exist only when an invite is redeemed, and every failure path
(wrong key, reused invite, expired invite, open signup) is closed.
"""

from datetime import datetime, timedelta, timezone

from tests.conftest import TEST_OPERATOR_KEY

OP = {"X-Operator-Key": TEST_OPERATOR_KEY}


def provision(client, tenant_id="acme", name="Acme Corp",
              admin_email="admin@acme.com"):
    r = client.post("/api/ops/tenants", headers=OP, json={
        "tenant_id": tenant_id, "name": name, "admin_email": admin_email,
    })
    assert r.status_code == 201, r.text
    return r.json()


def redeem(client, invite_token, password="hunter22!", display_name="Someone"):
    r = client.post("/api/auth/register", json={
        "invite_token": invite_token, "display_name": display_name,
        "password": password,
    })
    return r


class TestOperatorGate:
    def test_no_key_header_is_401(self, client):
        r = client.post("/api/ops/tenants", json={
            "tenant_id": "x2", "name": "X", "admin_email": "a@b.co"})
        assert r.status_code == 401

    def test_wrong_key_is_401(self, client):
        r = client.get("/api/ops/tenants", headers={"X-Operator-Key": "nope"})
        assert r.status_code == 401

    def test_unconfigured_server_disables_ops(self, client, monkeypatch):
        from app.config import get_settings
        monkeypatch.delenv("OPERATOR_KEY", raising=False)
        get_settings.cache_clear()
        try:
            r = client.get("/api/ops/tenants", headers=OP)
            assert r.status_code == 503
        finally:
            monkeypatch.setenv("OPERATOR_KEY", TEST_OPERATOR_KEY)
            get_settings.cache_clear()


class TestProvisionTenant:
    def test_returns_all_credentials_once(self, client):
        d = provision(client)
        assert d["tenant_id"] == "acme"
        assert len(d["punchout_secret"]) > 20
        assert len(d["po_key"]) > 20
        assert d["admin_invite"]["email"] == "admin@acme.com"
        assert d["admin_invite"]["invite_token"]

    def test_duplicate_tenant_409(self, client):
        provision(client)
        r = client.post("/api/ops/tenants", headers=OP, json={
            "tenant_id": "acme", "name": "Acme again", "admin_email": "x@acme.com"})
        assert r.status_code == 409

    def test_reserved_and_malformed_ids_rejected(self, client):
        for bad in ["admin", "api", "vendor", "a", "has space", "-lead"]:
            r = client.post("/api/ops/tenants", headers=OP, json={
                "tenant_id": bad, "name": "X", "admin_email": "a@b.co"})
            assert r.status_code == 422, f"{bad!r} should be rejected"

    def test_uppercase_id_is_normalized_not_rejected(self, client):
        r = client.post("/api/ops/tenants", headers=OP, json={
            "tenant_id": "UpperCo", "name": "Upper Co", "admin_email": "a@b.co"})
        assert r.status_code == 201
        assert r.json()["tenant_id"] == "upperco"

    def test_list_tenants(self, client):
        provision(client)
        provision(client, tenant_id="globex", name="Globex",
                  admin_email="admin@globex.com")
        rows = client.get("/api/ops/tenants", headers=OP).json()
        ids = {t["tenant_id"] for t in rows}
        assert {"acme", "globex"} <= ids
        acme = next(t for t in rows if t["tenant_id"] == "acme")
        assert acme["punchout_secret_configured"] is True
        assert acme["po_key_configured"] is True


class TestInviteRegistration:
    def test_happy_path_creates_admin(self, client):
        d = provision(client)
        r = redeem(client, d["admin_invite"]["invite_token"], display_name="Ada")
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["user"]["tenant_id"] == "acme"
        assert body["user"]["email"] == "admin@acme.com"
        assert body["user"]["role"] == "admin"
        assert body["token"]

    def test_invite_is_single_use(self, client):
        d = provision(client)
        token = d["admin_invite"]["invite_token"]
        assert redeem(client, token).status_code == 201
        r = redeem(client, token, password="different-pass1!")
        assert r.status_code == 422
        assert "not recognized" in r.json()["detail"]

    def test_garbage_token_rejected_with_same_error(self, client):
        r = redeem(client, "totally-made-up-token")
        assert r.status_code == 422
        assert "not recognized" in r.json()["detail"]

    def test_expired_invite_rejected(self, client):
        from app.db import get_db
        from app.main import app
        from app.models import StaffInvite

        d = provision(client)
        # Backdate the invite past its TTL straight in the DB.
        gen = app.dependency_overrides[get_db]()
        db = next(gen)
        invite = db.query(StaffInvite).first()
        invite.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()
        r = redeem(client, d["admin_invite"]["invite_token"])
        assert r.status_code == 422

    def test_open_registration_is_gone(self, client):
        # The old body shape (tenant bootstrap) must not create anything.
        r = client.post("/api/auth/register", json={
            "tenant_id": "squatter", "email": "a@b.co", "password": "hunter22!"})
        assert r.status_code == 422  # missing invite_token → validation error
        r2 = client.post("/api/auth/login", json={
            "tenant_id": "squatter", "email": "a@b.co", "password": "hunter22!"})
        assert r2.status_code == 401

    def test_weak_password_rejected_token_survives(self, client):
        d = provision(client)
        token = d["admin_invite"]["invite_token"]
        assert redeem(client, token, password="short").status_code == 422
        # The failed attempt must not burn the invite.
        assert redeem(client, token).status_code == 201


class TestAdminInvites:
    def _admin(self, client):
        d = provision(client)
        r = redeem(client, d["admin_invite"]["invite_token"])
        return {"Authorization": "Bearer " + r.json()["token"]}

    def test_admin_invites_member_with_privileges(self, client):
        hdr = self._admin(client)
        r = client.post("/api/auth/invites", headers=hdr, json={
            "email": "uploader@acme.com", "role": "member",
            "privileges": ["catalog_upload"]})
        assert r.status_code == 201, r.text
        token = r.json()["invite_token"]
        reg = redeem(client, token, display_name="Upl")
        assert reg.status_code == 201
        user = reg.json()["user"]
        assert user["role"] == "member"
        assert user["privileges"] == ["catalog_upload"]

    def test_unknown_privilege_rejected(self, client):
        hdr = self._admin(client)
        r = client.post("/api/auth/invites", headers=hdr, json={
            "email": "x@acme.com", "privileges": ["rule_the_world"]})
        assert r.status_code == 422

    def test_member_cannot_issue_invites(self, client):
        hdr = self._admin(client)
        r = client.post("/api/auth/invites", headers=hdr, json={
            "email": "m@acme.com", "role": "member", "privileges": []})
        mtoken = redeem(client, r.json()["invite_token"]).json()["token"]
        r2 = client.post("/api/auth/invites",
                         headers={"Authorization": "Bearer " + mtoken},
                         json={"email": "y@acme.com"})
        assert r2.status_code == 403

    def test_list_and_revoke(self, client):
        hdr = self._admin(client)
        r = client.post("/api/auth/invites", headers=hdr,
                        json={"email": "gone@acme.com"})
        invite_id = r.json()["id"]
        pending = client.get("/api/auth/invites", headers=hdr).json()
        assert any(i["id"] == invite_id for i in pending)
        assert client.delete(f"/api/auth/invites/{invite_id}",
                             headers=hdr).status_code == 200
        # Revoked token no longer registers.
        assert redeem(client, r.json()["invite_token"]).status_code == 422

    def test_reinvite_replaces_pending(self, client):
        hdr = self._admin(client)
        r1 = client.post("/api/auth/invites", headers=hdr,
                         json={"email": "twice@acme.com"})
        r2 = client.post("/api/auth/invites", headers=hdr,
                         json={"email": "twice@acme.com"})
        assert redeem(client, r1.json()["invite_token"]).status_code == 422
        assert redeem(client, r2.json()["invite_token"]).status_code == 201

    def test_ops_reinvite_for_lost_link(self, client):
        provision(client)  # admin invite never redeemed — "lost"
        r = client.post("/api/ops/tenants/acme/invites", headers=OP,
                        json={"email": "admin@acme.com", "role": "admin"})
        assert r.status_code == 201
        assert redeem(client, r.json()["invite_token"]).status_code == 201

    def test_invite_preview(self, client):
        d = provision(client)
        token = d["admin_invite"]["invite_token"]
        r = client.get("/api/auth/invites/preview", params={"token": token})
        assert r.status_code == 200
        assert r.json()["email"] == "admin@acme.com"
        assert r.json()["tenant_id"] == "acme"
        assert client.get("/api/auth/invites/preview",
                          params={"token": "junk"}).status_code == 404

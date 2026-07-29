"""Vendor accounts: invite flow, login, and the one-login-many-customers queue."""

import pytest

from app.models import Supplier, Tenant, VendorOrg, VendorUser
from app.services.auth import AuthError
from app.services.vendor_auth import (
    hash_invite, login_vendor, new_invite_code, register_vendor_user,
)


@pytest.fixture()
def org_with_invite(db):
    org = VendorOrg(name="ACME Corp", invite_code_hash=hash_invite("INVITE-1"))
    db.add(org)
    db.commit()
    return org


class TestRegistration:
    def test_register_with_invite(self, db, org_with_invite):
        user = register_vendor_user(db, "INVITE-1", "V@Acme.com", "Vera", "hunter22!")
        assert user.email == "v@acme.com"
        assert user.vendor_org_id == org_with_invite.id
        # Invite is single-use.
        db.refresh(org_with_invite)
        assert org_with_invite.invite_code_hash is None
        with pytest.raises(AuthError):
            register_vendor_user(db, "INVITE-1", "w@acme.com", "W", "hunter22!")

    def test_bad_invite_rejected(self, db, org_with_invite):
        with pytest.raises(AuthError):
            register_vendor_user(db, "WRONG", "v@acme.com", "V", "hunter22!")

    def test_weak_password_rejected(self, db, org_with_invite):
        with pytest.raises(AuthError):
            register_vendor_user(db, "INVITE-1", "v@acme.com", "V", "short")


class TestLogin:
    def test_login_roundtrip(self, db, org_with_invite):
        register_vendor_user(db, "INVITE-1", "v@acme.com", "V", "hunter22!")
        token, user = login_vendor(db, "v@acme.com", "hunter22!")
        assert len(token) == 64
        assert user.email == "v@acme.com"

    def test_wrong_password_generic_error(self, db, org_with_invite):
        register_vendor_user(db, "INVITE-1", "v@acme.com", "V", "hunter22!")
        with pytest.raises(AuthError, match="Wrong email or password"):
            login_vendor(db, "v@acme.com", "nope-nope")
        with pytest.raises(AuthError, match="Wrong email or password"):
            login_vendor(db, "ghost@acme.com", "whatever!")


class TestNetworkModel:
    def test_one_org_serves_many_customers(self, db):
        """The moat: one vendor org linked from suppliers in two tenants."""
        org = VendorOrg(name="ACME Corp", invite_code_hash=hash_invite("I"))
        db.add_all([Tenant(id="c1", name="Customer 1"), Tenant(id="c2", name="Customer 2"), org])
        db.flush()
        db.add_all([
            Supplier(tenant_id="c1", code="ACME", name="ACME", vendor_org_id=org.id),
            Supplier(tenant_id="c2", code="ACME-2", name="ACME", vendor_org_id=org.id),
        ])
        db.commit()

        linked = db.query(Supplier).filter_by(vendor_org_id=org.id).all()
        assert {s.tenant_id for s in linked} == {"c1", "c2"}

    def test_invite_code_generator_is_unique(self):
        codes = {new_invite_code() for _ in range(50)}
        assert len(codes) == 50

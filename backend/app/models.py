"""All persistent models: tenants, suppliers, contracts, catalog, price tiers,
policy, and punchout sessions.

Conventions:
- String UUID primary keys (hex, no dashes) except Tenant, which uses the
  caller-chosen slug so URLs and payloads stay readable.
- Every row carries tenant_id. Local mode trusts the request parameter;
  production derives it from the authenticated session (see README).
- Enums are stored by value (lowercase strings) so raw SQL stays legible.
- Every FK has a matching relationship(): SQLAlchemy only orders cross-table
  INSERTs within a flush along relationship() edges, not raw FK columns.
  SQLite (FKs unenforced) hid this; Postgres rejects children before parents.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean, Date, DateTime, Enum as SAEnum, Float, ForeignKey, Integer,
    JSON, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _enum(e):
    return SAEnum(e, values_callable=lambda x: [m.value for m in x], native_enum=False)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Tenancy and suppliers
# --------------------------------------------------------------------------

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # SHA-256 of the PO-receipt integration key SAP sends on /api/v1/po/receive.
    # Set/rotated by an admin; the plaintext is shown exactly once.
    po_key_hash: Mapped[str | None] = mapped_column(String(64))
    # SHA-256 of the punchout front-door credential SAP sends on /oci/start
    # (a fixed PASSWORD parameter in the SPRO catalog call structure). NULL
    # means punchout is closed for this tenant — there is no open fallback.
    punchout_secret_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class VendorOrg(Base):
    """Global vendor identity — the network model. A vendor registers once on
    the platform; per-tenant Supplier rows link here so one vendor login can
    serve every customer that buys from them. Tenant-scoped data (contracts,
    prices, catalogs) stays on the per-tenant rows."""

    __tablename__ = "vendor_orgs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(String(200), unique=True)
    # SHA-256 of the single-use invite code a customer admin hands the vendor;
    # consumed (nulled) by the first vendor-user registration.
    invite_code_hash: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SupplierProtocol(str, enum.Enum):
    CXML = "cxml"
    OCI = "oci"
    HOSTED = "hosted"


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    # Optional link to the vendor's global platform identity (network model).
    vendor_org_id: Mapped[str | None] = mapped_column(ForeignKey("vendor_orgs.id"))
    tenant: Mapped[Tenant] = relationship()
    vendor_org: Mapped[VendorOrg | None] = relationship()
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    sap_vendor_no: Mapped[str | None] = mapped_column(String(20))
    protocol: Mapped[SupplierProtocol] = mapped_column(
        _enum(SupplierProtocol), default=SupplierProtocol.HOSTED
    )

    # cXML punchout credentials. The shared secret is stored encrypted only.
    punchout_url: Mapped[str | None] = mapped_column(String(500))
    from_domain: Mapped[str | None] = mapped_column(String(60))
    from_identity: Mapped[str | None] = mapped_column(String(120))
    to_domain: Mapped[str | None] = mapped_column(String(60))
    to_identity: Mapped[str | None] = mapped_column(String(120))
    shared_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    deployment_mode: Mapped[str] = mapped_column(String(20), default="test")

    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # The confirmed column mapping for this supplier's file style.
    # Written once after human confirmation; load #2 onward is zero-touch.
    confirmed_mapping: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Contract(Base):
    __tablename__ = "contracts"
    __table_args__ = (UniqueConstraint("tenant_id", "contract_no"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    supplier_id: Mapped[str | None] = mapped_column(ForeignKey("suppliers.id"))
    tenant: Mapped[Tenant] = relationship()
    supplier: Mapped[Supplier | None] = relationship()
    contract_no: Mapped[str] = mapped_column(String(60))
    sap_agreement_no: Mapped[str | None] = mapped_column(String(20))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    # Higher wins when two contracts both cover an item.
    precedence: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    def is_valid_on(self, on: date) -> bool:
        return self.active and self.valid_from <= on <= self.valid_to


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------

class VersionStatus(str, enum.Enum):
    LOADED = "loaded"          # ingested, in review
    PUBLISHED = "published"    # live for punchout
    SUPERSEDED = "superseded"  # replaced by a newer published version


class ItemState(str, enum.Enum):
    AUTO_APPROVED = "auto_approved"  # confidence >= 0.95, publishes on release
    NEEDS_REVIEW = "needs_review"    # 0.70-0.95, one-click accept in the queue
    MANUAL = "manual"                # < 0.70 or hard-fail, needs a human
    APPROVED = "approved"            # human said yes
    REJECTED = "rejected"            # human said no


class CatalogVersion(Base):
    __tablename__ = "catalog_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.id"))
    tenant: Mapped[Tenant] = relationship()
    supplier: Mapped[Supplier] = relationship()
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[VersionStatus] = mapped_column(
        _enum(VersionStatus), default=VersionStatus.LOADED
    )
    source_filename: Mapped[str | None] = mapped_column(String(300))
    # What the mapper decided for this load, kept for audit even after the
    # supplier-level confirmed_mapping is written.
    mapping_used: Mapped[dict | None] = mapped_column(JSON)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_by: Mapped[str | None] = mapped_column(String(120))

    items: Mapped[list["CatalogItem"]] = relationship(back_populates="version")


class CatalogItem(Base):
    __tablename__ = "catalog_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    version_id: Mapped[str] = mapped_column(ForeignKey("catalog_versions.id"))
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.id"))
    tenant: Mapped[Tenant] = relationship()
    supplier: Mapped[Supplier] = relationship()

    supplier_part_id: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(String(40))   # SAP TXZ01 limit
    long_description: Mapped[str | None] = mapped_column(Text)
    manufacturer: Mapped[str | None] = mapped_column(String(120))
    manufacturer_part_id: Mapped[str | None] = mapped_column(String(120))

    uom_raw: Mapped[str | None] = mapped_column(String(30))
    uom_sap: Mapped[str | None] = mapped_column(String(10))
    unspsc: Mapped[str | None] = mapped_column(String(12))
    material_group: Mapped[str | None] = mapped_column(String(20))

    unit_price: Mapped[float | None] = mapped_column(Numeric(15, 4))
    price_unit: Mapped[int] = mapped_column(Integer, default=1)
    currency: Mapped[str | None] = mapped_column(String(3))
    lead_time_days: Mapped[int | None] = mapped_column(Integer)

    state: Mapped[ItemState] = mapped_column(_enum(ItemState), default=ItemState.MANUAL)
    confidence: Mapped[float | None] = mapped_column(Float)
    review_reasons: Mapped[list | None] = mapped_column(JSON)   # why it was queued
    suggestions: Mapped[list | None] = mapped_column(JSON)      # top candidates
    price_flags: Mapped[list | None] = mapped_column(JSON)      # surveillance output
    raw_row: Mapped[dict | None] = mapped_column(JSON)          # source row, for audit

    decided_by: Mapped[str | None] = mapped_column(String(120))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)

    version: Mapped[CatalogVersion] = relationship(back_populates="items")

    # services/pricing.py resolves against `list_price`; the catalog stores the
    # file's unit price under that meaning.
    @property
    def list_price(self):
        return self.unit_price


# --------------------------------------------------------------------------
# Price tiers
# --------------------------------------------------------------------------

class PriceTier(Base):
    __tablename__ = "price_tiers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    item_id: Mapped[str] = mapped_column(ForeignKey("catalog_items.id"))
    contract_id: Mapped[str | None] = mapped_column(ForeignKey("contracts.id"))
    tenant: Mapped[Tenant] = relationship()
    item: Mapped[CatalogItem] = relationship()
    contract: Mapped[Contract | None] = relationship()

    min_qty: Mapped[int] = mapped_column(Integer)
    max_qty: Mapped[int | None] = mapped_column(Integer)  # None = open-ended top tier
    unit_price: Mapped[float] = mapped_column(Numeric(15, 4))
    price_unit: Mapped[int] = mapped_column(Integer, default=1)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    def covers_qty(self, qty: int) -> bool:
        return qty >= self.min_qty and (self.max_qty is None or qty <= self.max_qty)

    def valid_on(self, on: date) -> bool:
        if self.valid_from and on < self.valid_from:
            return False
        if self.valid_to and on > self.valid_to:
            return False
        return True


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

class PolicyStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class PolicyAction(str, enum.Enum):
    ALLOW = "allow"
    WARN = "warn"
    ROUTE_APPROVAL = "route_approval"
    BLOCK = "block"


# The most severe fired action decides the line's outcome.
ACTION_SEVERITY: dict[PolicyAction, int] = {
    PolicyAction.ALLOW: 0,
    PolicyAction.WARN: 1,
    PolicyAction.ROUTE_APPROVAL: 2,
    PolicyAction.BLOCK: 3,
}


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[PolicyStatus] = mapped_column(
        _enum(PolicyStatus), default=PolicyStatus.DRAFT
    )
    contract_id: Mapped[str | None] = mapped_column(ForeignKey("contracts.id"))
    tenant: Mapped[Tenant] = relationship()
    contract: Mapped[Contract | None] = relationship()

    # What the author asked for vs. what activation granted after the clamp.
    requested_from: Mapped[date] = mapped_column(Date)
    requested_to: Mapped[date] = mapped_column(Date)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    clamped: Mapped[bool] = mapped_column(Boolean, default=False)
    clamp_note: Mapped[str | None] = mapped_column(Text)

    source_document: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    activated_by: Mapped[str | None] = mapped_column(String(120))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)

    rules: Mapped[list["PolicyRule"]] = relationship(back_populates="policy")

    def is_effective_on(self, on: date) -> bool:
        return (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_from <= on <= self.effective_to
        )


class PolicyRule(Base):
    __tablename__ = "policy_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    policy_id: Mapped[str] = mapped_column(ForeignKey("policies.id"))
    tenant: Mapped[Tenant] = relationship()
    code: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    condition: Mapped[dict] = mapped_column(JSON, default=dict)
    action: Mapped[PolicyAction] = mapped_column(
        _enum(PolicyAction), default=PolicyAction.WARN
    )
    route_to: Mapped[str | None] = mapped_column(String(120))
    severity: Mapped[str] = mapped_column(String(20), default="medium")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # When a model drafted this rule from a policy document, keep the receipts.
    source_clause: Mapped[str | None] = mapped_column(String(120))
    source_quote: Mapped[str | None] = mapped_column(Text)
    draft_confidence: Mapped[float | None] = mapped_column(Float)

    policy: Mapped[Policy] = relationship(back_populates="rules")


class PolicyEvaluation(Base):
    __tablename__ = "policy_evaluations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    tenant: Mapped[Tenant] = relationship()
    cart_ref: Mapped[str | None] = mapped_column(String(120))
    line_ref: Mapped[str | None] = mapped_column(String(120))
    policy_id: Mapped[str | None] = mapped_column(String(32))
    policy_version_no: Mapped[int | None] = mapped_column(Integer)
    fired_rules: Mapped[list | None] = mapped_column(JSON)
    evaluated_fields: Mapped[dict | None] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# --------------------------------------------------------------------------
# Users and privileges
# --------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    ADMIN = "admin"    # implies every privilege, can manage users
    MEMBER = "member"  # has exactly the privileges an admin granted


# The privilege vocabulary. An admin assigns any subset to a member.
PRIVILEGES = [
    "manage_users",        # create/edit users and their privileges
    "manage_master_data",  # suppliers and contracts
    "catalog_upload",      # load supplier files
    "catalog_review",      # approve / reject / edit queued rows
    "catalog_publish",     # release a version to the storefront
    "pricing_manage",      # upload price tiers
    "policy_manage",       # upload and activate policies
]


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    tenant: Mapped[Tenant] = relationship()
    email: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(120))
    # PBKDF2-HMAC-SHA256, 600k iterations, per-user random salt.
    password_salt: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(64))
    role: Mapped[UserRole] = mapped_column(_enum(UserRole), default=UserRole.MEMBER)
    privileges: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Brute-force protection: lock the account after repeated failures.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    def has_privilege(self, privilege: str) -> bool:
        return self.role is UserRole.ADMIN or privilege in (self.privileges or [])


class StaffInvite(Base):
    """Single-use, email-bound invitation to create a staff account.

    Registration is invite-only: there is no open signup and no
    register-bootstraps-a-tenant path. The token is generated by an operator
    (tenant provisioning) or a tenant admin (Users panel), carries the tenant,
    email, role, and privileges, and dies on first use. Only its SHA-256 is
    stored — the plaintext is shown exactly once at creation."""

    __tablename__ = "staff_invites"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    tenant: Mapped[Tenant] = relationship()
    email: Mapped[str] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(_enum(UserRole), default=UserRole.MEMBER)
    privileges: Mapped[list] = mapped_column(JSON, default=list)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[str | None] = mapped_column(String(200))  # admin email or "operator"
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    # Only the SHA-256 of the token is stored — a leaked database cannot be
    # replayed as live sessions. The client holds the only copy of the token.
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    user: Mapped[User] = relationship()
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# --------------------------------------------------------------------------
# Punchout sessions
# --------------------------------------------------------------------------

class PunchoutSession(Base):
    __tablename__ = "punchout_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    tenant: Mapped[Tenant] = relationship()
    protocol: Mapped[str] = mapped_column(String(10), default="oci")  # oci | cxml
    oci_version: Mapped[str | None] = mapped_column(String(5))
    # OCI: the SAP HOOK_URL the cart posts back to. Stored encrypted at rest.
    hook_url_encrypted: Mapped[str | None] = mapped_column(Text)
    # cXML: BuyerCookie round-trips through the supplier session.
    buyer_cookie: Mapped[str | None] = mapped_column(String(120))
    sap_user: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | returned | expired
    cart: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime)


# --------------------------------------------------------------------------
# Orders — POs received from the customer's S/4, tracked through fulfillment
# --------------------------------------------------------------------------

class OrderStatus(str, enum.Enum):
    RECEIVED = "received"        # PO landed from SAP, matched to catalog/contract
    ACKNOWLEDGED = "acknowledged"  # vendor confirmed they will fulfill
    SHIPPED = "shipped"          # vendor posted shipment + tracking
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class PriceVerdict(str, enum.Enum):
    """Line-level price truth: paid vs. what the contract says (the product's
    core promise — every PO line is verified against the negotiated price)."""

    MATCH = "match"              # within tolerance of the contracted price
    OVERPAID = "overpaid"
    UNDERPAID = "underpaid"
    OFF_CATALOG = "off_catalog"  # part not found in the published catalog


class Order(Base):
    __tablename__ = "orders"
    # Idempotency (SCALE.md D4): SAP re-pushes update in place, never duplicate.
    __table_args__ = (UniqueConstraint("tenant_id", "sap_po_number"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    supplier_id: Mapped[str | None] = mapped_column(ForeignKey("suppliers.id"))
    tenant: Mapped[Tenant] = relationship()
    supplier: Mapped[Supplier | None] = relationship()

    sap_po_number: Mapped[str] = mapped_column(String(35))  # EBELN is 10, cushion
    sap_po_version: Mapped[int] = mapped_column(Integer, default=1)  # bumps per re-push
    status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus), default=OrderStatus.RECEIVED, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    total: Mapped[float | None] = mapped_column(Numeric(15, 2))
    ordered_at: Mapped[date | None] = mapped_column(Date)  # PO document date
    source: Mapped[str] = mapped_column(String(20), default="json")  # json | idoc
    raw_payload_key: Mapped[str | None] = mapped_column(String(300))  # storage seam

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    lines: Mapped[list["OrderLine"]] = relationship(back_populates="order")
    events: Mapped[list["OrderEvent"]] = relationship(back_populates="order")


class OrderLine(Base):
    __tablename__ = "order_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    tenant: Mapped[Tenant] = relationship()
    order: Mapped[Order] = relationship(back_populates="lines")

    line_no: Mapped[int] = mapped_column(Integer)
    supplier_part_id: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(String(200))
    quantity: Mapped[float] = mapped_column(Numeric(15, 3), default=1)
    uom: Mapped[str | None] = mapped_column(String(10))
    unit_price: Mapped[float] = mapped_column(Numeric(15, 4))  # what SAP says was agreed
    currency: Mapped[str | None] = mapped_column(String(3))

    # Price-truth verification, filled at receipt time.
    item_id: Mapped[str | None] = mapped_column(ForeignKey("catalog_items.id"))
    contract_id: Mapped[str | None] = mapped_column(ForeignKey("contracts.id"))
    item: Mapped["CatalogItem | None"] = relationship()
    contract: Mapped[Contract | None] = relationship()
    expected_unit_price: Mapped[float | None] = mapped_column(Numeric(15, 4))
    price_verdict: Mapped[PriceVerdict | None] = mapped_column(_enum(PriceVerdict))
    price_delta: Mapped[float | None] = mapped_column(Numeric(15, 2))  # (paid-expected)*qty


class OrderEvent(Base):
    """The tracking timeline: received → acknowledged → shipped → delivered.
    Every state change and note is a row — 'where is my order?' is a SELECT."""

    __tablename__ = "order_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    tenant: Mapped[Tenant] = relationship()
    order: Mapped[Order] = relationship(back_populates="events")

    type: Mapped[str] = mapped_column(String(30))  # received|updated|acknowledged|shipped|delivered|cancelled|note
    data: Mapped[dict | None] = mapped_column(JSON)  # e.g. {"tracking_number":..., "carrier":...}
    actor: Mapped[str | None] = mapped_column(String(200))  # email or "sap"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# --------------------------------------------------------------------------
# Vendor users — one login per vendor org, serving every connected customer
# --------------------------------------------------------------------------

class VendorUser(Base):
    __tablename__ = "vendor_users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    vendor_org_id: Mapped[str] = mapped_column(ForeignKey("vendor_orgs.id"))
    vendor_org: Mapped[VendorOrg] = relationship()
    email: Mapped[str] = mapped_column(String(200), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_salt: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class VendorToken(Base):
    __tablename__ = "vendor_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    vendor_user_id: Mapped[str] = mapped_column(ForeignKey("vendor_users.id"))
    vendor_user: Mapped[VendorUser] = relationship()
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# --------------------------------------------------------------------------
# Background jobs
# --------------------------------------------------------------------------

class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"      # will be retried until attempts == max_attempts
    DEAD = "dead"          # exhausted retries; needs a human


class Job(Base):
    """Durable work queue (SCALE.md D5). Long-running work (big ingestions,
    leakage audits, vendor delivery) is enqueued here and executed by the
    worker loop in services/jobs.py, never inside an HTTP request. Failures
    are rows, not log lines: every attempt and error is queryable."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    tenant: Mapped[Tenant] = relationship()
    kind: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus), default=JobStatus.QUEUED, index=True
    )
    payload: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    created_by: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

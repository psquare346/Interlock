# Customer onboarding playbook

How a new buying company goes from "signed" to "punching out from their SAP."
Written so a non-founder hire can run phases 1–3 and hand phases 4–5 to the
customer's own IT. Every credential mentioned here is created once by the
platform and shown exactly once — store them in a password manager.

## Who is who

- **Shoppers (SAP requisitioners)** — never get Interlock accounts. They reach
  the catalog through their own SAP (ME51N/ME21N → the catalog button). Their
  identity rides the OCI call; there is nothing to provision per shopper.
- **Tenant staff** — the handful of catalog/procurement people who run the
  admin console. Invite-only accounts (see phase 3).
- **Vendors** — suppliers who acknowledge/ship orders in the `/vendor` portal.
  Invite-only, one login serves every customer they supply.
- **Operator (us)** — provisions tenants and issues the first admin invite.

## Separation between customers

Each company is a **tenant**. Every row (catalog, contract, price, PO, user,
session) carries `tenant_id`; every query filters on it. Admin tokens *derive*
the tenant — an Acme admin cannot name Globex. The punchout front door is
credentialed per tenant (below), so a stranger who guesses a tenant id still
cannot open its catalog. Nobody buys *through* us: the cart posts back into the
customer's own SAP, where their approval flow and PO happen.

---

## Phase 0 — one-time platform readiness (before customer #1)

Blocking items from [GO-LIVE.md](GO-LIVE.md) 1–4: real Postgres, TLS on a real
domain, secrets backend, fresh `ENCRYPTION_KEY`. Plus set `OPERATOR_KEY` on the
server (Render dashboard → Environment) — it gates tenant provisioning and is
your master onboarding credential. Keep it out of the repo and out of email.

## Phase 1 — the sale (salesperson; no system access)

Collect a one-page fact sheet — everything later depends on it:

- S/4HANA version, and **SAP GUI version on requisitioners' desktops**
  (WebView2 vs the old IE engine — item 1 on the SAP-CONNECT pre-flight list,
  the thing most likely to sink a first demo).
- Their Basis/functional contact (does the SPRO wiring in phase 4).
- Supplier list: which vendors, rough item counts, tier pricing or not.
- Network posture: SAP egress IPs (for our allowlist), any desktop proxy.

## Phase 2 — provision the tenant (operator; ~1 minute)

One command creates the tenant, its punchout secret, its PO key, and the first
admin's invite link:

```
python backend/scripts/provision_tenant.py \
    --base https://<your-domain> \
    --operator-key <OPERATOR_KEY> \
    --tenant acme --name "Acme Corp" --admin-email admin@acme.com
```

It prints two bundles:

- **To the customer's admin:** the single-use register link
  (`/admin#invite=…`). They set their own password; no tenant id is ever typed.
- **To the customer's Basis team:** the web-catalog URL, the `PASSWORD` value
  (punchout secret), the PO-receive URL, and the `X-PO-Key`.

Lost admin link, or need a second admin? `--reinvite someone@acme.com`.
Rotate the punchout secret later? `--rotate-punchout-secret` (or the tenant
admin does it in the console). Tenant ids are assigned by us, lowercase,
immutable, and validated against a reserved-name list — customers see their
company *name* everywhere; the id is internal plumbing.

## Phase 3 — load the catalog (customer staff, or us white-glove)

The customer's admin redeems the invite, then adds staff from the Users tab
(invite-only, with per-person privileges — uploader ≠ reviewer ≠ publisher, so
duties can be split). Then, in order (each step validates against the previous):
supplier → contract → catalog upload → review/publish → tier pricing → policy.
For a pilot, expect to do this with them on a screen-share — it is also where
they first see the review/approval gate, which is part of the value.

## Phase 4 — SAP wiring (customer's Basis; 1–2 hrs in their sandbox)

Follow [SAP-CONNECT.md](SAP-CONNECT.md). Wire 1 (catalog inside SAP): the SPRO
web-service entry now has **four** call-structure rows — the extra one is the
`PASSWORD` fixed parameter carrying the punchout secret. Run the pre-flight
checklist; do the browser sanity-test URL first to split "SAP config wrong"
from "secret wrong". Wire 2 (PO feed): WE21/SM59 with the `X-PO-Key`. Repeat
the SPRO entry in production once the sandbox round trip is clean.

## Phase 5 — vendor activation (us + suppliers)

Single-use invite per supplier org (Setup tab → "Vendor invite"). Vendor
registers once at `/vendor`, then sees POs from every customer that links them.
Keep each supplier in `deployment_mode=test` until one clean round trip, then
flip to `production`.

## Phase 6 — first live weeks

The PO feed price-verifies every line against contract automatically. The first
"this PO paid $X over contract" report is when the customer sees why this is a
service, not a website.

---

## What still needs building (not blockers for a pilot, but name them honestly)

- Retain original uploaded catalog files for audit (today files are parsed and
  discarded; only the structured rows persist).
- Postgres row-level security on `tenant_id` (GO-LIVE item 3) — a database-level
  second wall behind the app-level checks. Do before customer #2.
- Real backups / point-in-time recovery (GO-LIVE item 12). Do before any real
  data lands.
- Product roadmap: Option B requisition-creation into S/4, order status back
  into SAP, per-vendor cXML/EDI, supplier self-service catalog editing.

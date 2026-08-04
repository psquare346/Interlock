# Customer onboarding playbook

How a new buying company goes from "signed" to "punching out from their SAP."
Written so a non-founder hire can run phases 1–3 and hand phases 4–5 to the
customer's own IT. Every credential mentioned here is created once by the
platform and shown exactly once — store them in a password manager.

The SAP-side click-by-click detail lives in [SAP-CONNECT.md](SAP-CONNECT.md);
this file is the orchestration (who does what, in what order). You need both
for a full SAP wiring. Read the "Prove it without SAP" box below before any of
the phases — you can demo the entire product before a customer's SAP is touched.

> **Prove it without SAP (do this first, for every demo).** The platform's own
> logic is fully testable with no SAP system at all, and this is your best
> sales tool:
> - **Punchout round trip** — open a credentialed punchout session and transfer
>   a cart into the built-in *simulated SAP receiver* (`/api/punchout/oci/mock-requisition`).
>   Catalog → cart → OCI transfer → requisition lines, in a browser.
> - **PO feed** — POST a sample order to `/api/v1/po/receive?tenant_id=…` (with
>   the `X-PO-Key`) from Postman/curl; watch it appear price-verified on `/orders`.
>
> A real SAP only ever proves the *SAP-side config* (the SPRO button, the
> embedded browser, ORDERS05 output). Everything on our side is provable today.

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
server — it gates tenant provisioning (and the `/ops` console) and is your
master onboarding credential. Keep it out of the repo and out of email.

Setting `OPERATOR_KEY` on Render: `render.yaml` declares it with
`generateValue: true`, but env vars added to the blueprint only take effect on
a **Blueprint sync**, not on an ordinary git-push deploy. Simplest reliable
path: in the Render dashboard open the **interlock** web service → **Environment**
→ add `OPERATOR_KEY` with a strong value you generate
(`python -c "import secrets; print(secrets.token_urlsafe(32))"`) → save (it
redeploys). Then `/ops` unlocks with that value.

## Phase 1 — the sale (salesperson; no system access)

Collect a one-page fact sheet — everything later depends on it:

- S/4HANA version, and **SAP GUI version on requisitioners' desktops**
  (WebView2 vs the old IE engine — item 1 on the SAP-CONNECT pre-flight list,
  the thing most likely to sink a first demo).
- Their Basis/functional contact (does the SPRO wiring in phase 4).
- Supplier list: which vendors, rough item counts, tier pricing or not.
- **Network reachability — confirm this early, it is the most common blocker.**
  Two *different* network paths, and they fail independently:
  - **Wire 1 (catalog button)** is opened by the *requisitioner's browser*, so
    it needs the **desktop/workstation** (through any corporate proxy) to reach
    our host. It does NOT need the SAP server to have internet.
  - **Wire 2 (PO feed)** is a *server-to-server* call, so the **SAP application
    server itself** must be able to make outbound HTTPS (443) to our host —
    directly or via a proxy configured in SM59. Many sandboxes cannot do this
    out of the box (`NIECONN_REFUSED` in SM59 is the classic symptom), and it
    takes a firewall/proxy change on the customer's side. Ask up front: "can
    your SAP application server make outbound HTTPS calls to the internet, and
    if so through what proxy?" Give their team our host + egress IP for the
    allowlist. Because Wire 1 doesn't need this, a customer can go live on the
    catalog while the PO-feed egress is still being arranged.

## Phase 2 — provision the tenant (operator; ~1 minute)

One step creates the tenant, its punchout secret, its PO key, and the first
admin's invite link. Two ways to run it — same underlying action:

**Web console (recommended):** go to **`/ops`**, unlock with the `OPERATOR_KEY`
(this is the operator's own door — not a tenant login, so it is deliberately
not linked from the tenant admin console). Fill the "Provision a new customer"
form (company id, name, admin email) and click. The page shows the credentials
with copy buttons, and a table of all customers that grows with each one. Per-
row buttons handle "Re-invite admin" and "Rotate secret."

**CLI (scriptable alternative):**

```
python backend/scripts/provision_tenant.py \
    --base https://<your-domain> \
    --operator-key <OPERATOR_KEY> \
    --tenant acme --name "Acme Corp" --admin-email admin@acme.com
```

Either way you get two bundles, each credential shown exactly once:

- **To the customer's admin:** the single-use register link
  (`/admin#invite=…`). They set their own password; no tenant id is ever typed.
- **To the customer's Basis team:** the web-catalog URL, the `PASSWORD` value
  (punchout secret), the PO-receive URL, and the `X-PO-Key`.

Lost admin link, or need a second admin? Re-invite (the `/ops` table button, or
`--reinvite someone@acme.com`). Rotate the punchout secret later? The `/ops`
"Rotate secret" button, `--rotate-punchout-secret`, or the tenant admin does it
themselves in the console. Tenant ids are assigned by us, lowercase, immutable,
and validated against a reserved-name list — customers see their company *name*
everywhere; the id is internal plumbing.

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
from "secret wrong". **Wire 1 is browser-based, so it does not need the SAP
server to reach the internet** — it can be wired and tested even where the PO
feed cannot yet.

Wire 2 (PO feed): SM59 (type G) → WE21 → WE20, with the `X-PO-Key`. If SM59's
connection test returns **`NIECONN_REFUSED`**, that is the phase-1 network
prerequisite biting — the SAP *server* can't reach our host — not a config
error. Test it independently from Postman/curl first (the "Prove it without
SAP" box), so a red SM59 test clearly means "server egress," and resolve that
with the customer's network team (open outbound 443, or set the SM59 proxy).
Repeat the SPRO entry in production once the sandbox round trip is clean.

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

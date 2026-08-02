# Connecting an SAP S/4HANA sandbox to Interlock

Two wires, independent of each other. Wire 1 makes the catalog appear inside
SAP. Wire 2 makes SAP push finished POs to the platform. Do them in that
order — Wire 1 needs only config a functional consultant can do in an
afternoon; Wire 2 touches output management.

**Placeholders:** `<BASE>` = your deployment, `<TENANT>` = your tenant id,
`<KEY>` = your PO key, `<SECRET>` = your punchout secret. **Substitute the
real value and drop the angle brackets** — never type `<` or `>` into SAP.
For the current deployment:

| Placeholder | Type this instead                                        |
|-------------|----------------------------------------------------------|
| `<BASE>`    | `https://interlock-s20b.onrender.com`                    |
| `<TENANT>`  | `demo`                                                   |
| `<KEY>`     | the key from the `/orders` page                          |
| `<SECRET>`  | the punchout secret from `/admin` → Setup (shown once)   |

---

## Wire 1 — OCI punchout (catalog inside SAP)

**Interlock side (do first):** supplier + contract created, catalog uploaded,
reviewed, **published**, tiers loaded — and a **punchout secret generated**
(`/admin` → Setup → "Generate / rotate punchout secret"; it is shown once).
The storefront is credentialed: `/oci/start` rejects any call that does not
carry the tenant's secret, so there is no longer an open items URL to test in
a bare browser tab — use the sanity-test URL below instead.

**SAP side — define the web service:**

IMG path: `SPRO → Materials Management → Purchasing → Environment →
Web Services: ID and Description` (the OCI catalog table). Create an entry:

- Web service ID: `INTERLOCK`, description: `Interlock punchout catalog`
- **Call structure** — exactly four rows, typed literally as shown
  (row 10 has no parameter name; row 40 has no value — SAP fills it):

| Seq | Parameter name | Parameter value                                                     | Type        |
|-----|----------------|---------------------------------------------------------------------|-------------|
| 10  | *(leave empty)* | `https://interlock-s20b.onrender.com/api/v1/punchout/oci/start`     | URL         |
| 20  | `tenant_id`    | `demo`                                                              | Fixed value |
| 30  | `PASSWORD`     | the punchout secret (no quotes)                                     | Fixed value |
| 40  | `HOOK_URL`     | *(leave empty)*                                                     | Return URL  |

Row 40's name must be spelled `HOOK_URL` in capitals — that is the OCI
standard name SAP looks for. Row 30 (`PASSWORD`, also capitals) is the
tenant's front-door credential: requisitioners never see it, and calls
without it get `401`. No trailing slash on the URL, no quotes anywhere, and
no `<` `>` characters.

**The switch that actually shows the button: set the Default Indicator on
the web service entry.** The button in `ME51N`/`ME21N` reads customizing
view `MMPUROCI_ENTIT_V` (the same IMG node; also reachable via `SM30`), and
with no entry flagged as default, **no catalog appears at all** — even with
a perfect call structure. Exactly one entry carries the flag; standard
`ME51N`/`ME21N` calls only that default catalog (multi-catalog selection
needs extra development around `MMPUROCI_CALL`, inactive by default).
Access can be restricted per user — see SAP KBA 3141117.

That is the whole OCI contract: SAP opens the URL with a generated
`HOOK_URL`; Interlock redirects the browser to the storefront; on transfer,
the cart posts `NEW_ITEM-*` fields back to the `HOOK_URL` and the lines land
in the requisition/PO item grid.

### Pre-flight checklist (the things that actually fail)

Menu paths vary by release — verify each in your own system rather than
trusting a path here. Ordered by how often they bite.

1. **The embedded browser in SAP GUI.** Classic `ME51N` opens the catalog in
   an embedded control. Older SAP GUI versions use an **Internet Explorer**
   engine, which will not run a modern storefront; SAP GUI 7.70+ uses the
   Chromium-based Edge WebView2 and works fine. Check your GUI version and
   the WebView2 setting first — if you are on an IE-based control, either
   test in Fiori/a real browser or ask and a legacy-compatible page can be
   built. **This is the most likely reason a correctly-configured punchout
   shows a blank or broken page.**
2. **The `PASSWORD` row in the call structure.** A blank page that is really
   a `401` usually means the punchout secret row is missing, misspelled
   (`PASSWORD` in capitals), carries quotes, or was rotated on the Interlock
   side after SPRO was configured. Re-run the sanity-test URL below with the
   current secret to split "SAP config wrong" from "secret wrong".
3. **Unit of measure must exist in the target system.** We send
   `NEW_ITEM-UNIT`. `EA` is universally present; `KAR`, `PAK`, `CAR` and
   friends are not, and a language-mismatched code (`EA` vs `ST`) fails the
   same way. The seeder ships EA-only for exactly this reason.
4. **Vendor number.** `NEW_ITEM-VENDOR` must be a vendor that exists and is
   extended to the purchasing org, or blank. The seeder leaves it blank;
   pass `--sap-vendor-no` only with a real test vendor.
5. **Material group.** `NEW_ITEM-MATGROUP` is only sent when the catalog row
   has one, and it must exist in the target system. The synthetic catalog
   leaves it empty deliberately.
6. **Currency** must be valid in the system and consistent with the contract.
7. **Account assignment.** Punchout lines arrive as free-text (material-less)
   requisition items, so the requester still supplies cost center / G/L —
   punchout does not and cannot fill those. Make sure a test user has
   defaults (purchasing org, plant, document type) that let a PR save.
8. **Network path from the *user's* workstation.** The browser — not the SAP
   server — calls the storefront, so the corporate proxy must allow the host.
   A brand-new domain is sometimes uncategorized and blocked by web filtering.
   The same browser must also be able to POST back to SAP's `HOOK_URL`, which
   is an internal URL: both paths have to work from that one machine.
9. **Popup/window behavior.** Punchout typically opens a new window; a popup
   blocker or a locked-down GUI theme can swallow it.
10. **Authorization to use catalogs.** If the Catalogs entry point does not
   appear at all for a test user despite the web service existing, this is
   usually authorization or a missing user default rather than the OCI config.

**Where the catalog button appears:**

- Classic GUI: `ME51N` / `ME21N` (and `ME52N`) show a **Catalogs** dropdown
  once a web service ID exists and is assigned. If it does not appear, the
  usual suspect is user parameter/authorization for catalog usage — a
  functional consultant fixes that in minutes.
- Fiori self-service requisitioning: assign the catalog (web service ID) in
  the catalog management config for the *Create Purchase Requisition* app.

**TLS note:** the punchout URL is opened by the **user's browser**, not by
the SAP server, so no STRUST work is needed for Wire 1. Render's certificate
is from a public CA that browsers already trust.

**Sanity test without SAP** — paste this in a browser (one line, real values
substituted for `<SECRET>`, no angle brackets):

```
https://interlock-s20b.onrender.com/api/punchout/oci/start?tenant_id=demo&PASSWORD=<SECRET>&HOOK_URL=https://interlock-s20b.onrender.com/api/punchout/oci/mock-requisition
```

You should land on the storefront; Transfer posts the cart to the simulated
SAP receiver, which renders the exact OCI fields SAP would receive as
requisition lines. If that works, the SAP config above is just pointing at
the same URL. A `401` here means the secret is wrong or was never generated;
a fresh one comes from `/admin` → Setup (rotating invalidates the old one).

---

## Wire 2 — PO feed (SAP pushes purchase orders to Interlock)

Endpoint: `POST <BASE>/api/v1/po/receive?tenant_id=<TENANT>`
Auth: the PO key — generate it on `<BASE>/orders` (shown once).
Accepted bodies: ORDERS05 IDoc-XML or the JSON shape in
`backend/samples/po_sample.json`.

The key travels either as header `X-PO-Key: <key>` or, where SAP config
cannot set headers, as query `&po_key=<key>`.

**Sandbox route (classic IDoc-over-HTTP, no middleware):**

1. `SM59` — RFC destination, type **G** (HTTP to external server):
   host your Render host, port 443, SSL active,
   path prefix `/api/v1/po/receive?tenant_id=<TENANT>&po_key=<KEY>`.
2. `STRUST` — import the CA chain of your host into the
   *SSL client (anonymous)* PSE so the SAP **server** trusts it
   (this is the server-to-server call, unlike Wire 1).
3. `WE21` — port of type **XML HTTP** using that RFC destination,
   content type `text/xml`.
4. `WE20` — partner profile for the vendor: outbound parameter,
   message type `ORDERS`, basic type `ORDERS05`, via the WE21 port.
5. Output determination: the PO's message/output record for that vendor
   uses medium EDI/ALE so saving a PO in `ME21N` emits the IDoc.

Then create a PO in `ME21N` for that vendor and watch it appear on
`<BASE>/orders` seconds later — matched to the supplier by SAP vendor
number, every line price-verified against the contract.

**Alternatives:** S/4's SOAP output (SOAMANAGER) pointing at the same URL
also works — the endpoint answers anything that POSTs ORDERS05 XML. If the
sandbox fights you, the demo fallback is pushing
`backend/samples/po_sample_orders05.xml` with curl — identical payload,
identical result.

**Idempotency:** re-outputs of the same PO update the existing order and
bump its version. Safe to fire repeatedly.

---

## Field mapping reference (what we read from ORDERS05)

| IDoc segment/field                 | Meaning              | Lands as            |
|------------------------------------|----------------------|---------------------|
| `E1EDK01/BELNR`                    | PO number            | order.sap_po_number |
| `E1EDK01/CURCY`                    | currency             | order.currency      |
| `E1EDK03[IDDAT=012]/DATUM`         | document date        | order.ordered_at    |
| `E1EDKA1[PARVW=LF]/PARTN`          | vendor number        | supplier match      |
| `E1EDP01/POSEX, MENGE, MENEE, NETPR` | line no, qty, UoM, net price | line fields |
| `E1EDP19[QUALF=002]/IDTNR, KTEXT`  | vendor part + text   | catalog match (001 fallback) |

Everything else in the IDoc is preserved verbatim in the stored raw payload.

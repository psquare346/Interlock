# Connecting an SAP S/4HANA sandbox to Interlock

Two wires, independent of each other. Wire 1 makes the catalog appear inside
SAP. Wire 2 makes SAP push finished POs to the platform. Do them in that
order — Wire 1 needs only config a functional consultant can do in an
afternoon; Wire 2 touches output management.

**Placeholders:** `<BASE>` = your deployment, `<TENANT>` = your tenant id,
`<KEY>` = your PO key. **Substitute the real value and drop the angle
brackets** — never type `<` or `>` into SAP. For the current deployment:

| Placeholder | Type this instead                     |
|-------------|---------------------------------------|
| `<BASE>`    | `https://interlock-s20b.onrender.com` |
| `<TENANT>`  | `demo`                                |
| `<KEY>`     | the key from the `/orders` page       |

---

## Wire 1 — OCI punchout (catalog inside SAP)

**Interlock side (do first):** supplier + contract created, catalog uploaded,
reviewed, **published**, tiers loaded. Test in a plain browser tab that
`<BASE>/api/catalog/items?tenant_id=<TENANT>` returns items.

**SAP side — define the web service:**

IMG path: `SPRO → Materials Management → Purchasing → Environment →
Web Services: ID and Description` (the OCI catalog table). Create an entry:

- Web service ID: `INTERLOCK`, description: `Interlock punchout catalog`
- **Call structure** — exactly three rows, typed literally as shown
  (row 10 has no parameter name; row 30 has no value — SAP fills it):

| Seq | Parameter name | Parameter value                                                     | Type        |
|-----|----------------|---------------------------------------------------------------------|-------------|
| 10  | *(leave empty)* | `https://interlock-s20b.onrender.com/api/v1/punchout/oci/start`     | URL         |
| 20  | `tenant_id`    | `demo`                                                              | Fixed value |
| 30  | `HOOK_URL`     | *(leave empty)*                                                     | Return URL  |

Row 30's name must be spelled `HOOK_URL` in capitals — that is the OCI
standard name SAP looks for. No trailing slash on the URL, no quotes
anywhere, and no `<` `>` characters.

That is the whole OCI contract: SAP opens the URL with a generated
`HOOK_URL`; Interlock redirects the browser to the storefront; on transfer,
the cart posts `NEW_ITEM-*` fields back to the `HOOK_URL` and the lines land
in the requisition/PO item grid.

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

**Sanity test without SAP** — paste this in a browser (one line, real values,
no placeholders):

```
https://interlock-s20b.onrender.com/api/punchout/oci/start?tenant_id=demo&HOOK_URL=https://interlock-s20b.onrender.com/api/punchout/oci/mock-hook
```

You should land on the storefront; Transfer posts the cart to the mock hook,
which echoes the exact OCI fields SAP would receive. If that works, the SAP
config above is just pointing at the same URL.

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

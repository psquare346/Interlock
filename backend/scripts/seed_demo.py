"""Seed a demo tenant with SYNTHETIC data — nothing confidential.

Builds a complete, believable scene through the public APIs: supplier,
contract, published catalog with quantity tiers, POs in several states
(including a deliberately overpaid line and an off-catalog line), a vendor
account that has acknowledged and shipped one order, and a finished leakage
audit. Safe to point at localhost or a deployment.

    python scripts/seed_demo.py --base http://localhost:8080 \
        --tenant demo --email you@example.com --password 'your-password'

The account must already exist (register it in the UI first) and be the
tenant's admin. Re-running is safe: existing objects are reused.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

# --- Synthetic vendor and catalog. Invented parts, plausible office prices. --
SUPPLIER = {"code": "NWOS", "name": "Northwind Office Supply",
            "sap_vendor_no": "0009100001", "protocol": "hosted",
            "deployment_mode": "test"}
CONTRACT_NO = "NWOS-FY26"

CATALOG_CSV = """supplier_part_id,description,uom,unit_price,currency,price_unit,unspsc,material_group,manufacturer,manufacturer_part_id,lead_time_days,long_description
NW-1001,Copy paper A4 80gsm white,KAR,32.40,USD,1,14111507,,Northwind,CP-A4-80,3,Carton of 5 reams (2500 sheets) multipurpose copy paper.
NW-1002,Ballpoint pen blue medium,PAK,4.85,USD,1,44121707,,Northwind,BP-BLU-M,2,Pack of 10 retractable ballpoint pens.
NW-1003,Toner cartridge black high yield,EA,89.50,USD,1,44103105,,Northwind,TN-9000XL,5,High-yield black toner approx 9000 pages.
NW-1004,File folder manila letter,PAK,11.20,USD,1,44122011,,Northwind,FF-MAN-100,4,Box of 100 manila file folders.
NW-1005,Whiteboard marker assorted,PAK,7.95,USD,1,44121708,,Northwind,WM-AST-4,3,Pack of 4 dry-erase markers.
NW-1006,Packing tape 48mm x 66m,EA,2.65,USD,1,31201503,,Northwind,PT-48-66,2,
"""

TIERS_CSV = """supplier_part_id,min_qty,max_qty,unit_price,price_unit,currency
NW-1001,1,9,32.40,1,USD
NW-1001,10,49,29.80,1,USD
NW-1001,50,,27.50,1,USD
NW-1002,1,19,4.85,1,USD
NW-1002,20,,4.20,1,USD
NW-1003,1,4,89.50,1,USD
NW-1003,5,,82.00,1,USD
NW-1006,1,23,2.65,1,USD
NW-1006,24,,2.20,1,USD
"""

# Deliberately mixed: contract-price lines, one clearly overpaid line, and a
# part that is not in the catalog at all — so the demo shows all verdicts.
PO_HISTORY_CSV = """po_number,date,part,quantity,unit_price
4500010001,2026-04-08,NW-1001,50,27.50
4500010002,2026-04-15,NW-1001,50,32.40
4500010003,2026-04-22,NW-1002,20,4.20
4500010004,2026-05-06,NW-1003,5,89.50
4500010005,2026-05-13,NW-1001,60,31.00
4500010006,2026-05-20,NW-1006,24,2.65
4500010007,2026-06-03,NW-9999,10,18.00
4500010008,2026-06-17,NW-1003,6,95.00
4500010009,2026-07-01,NW-1002,25,4.85
4500010010,2026-07-15,NW-1001,100,27.50
"""

ORDERS = [
    # (po_number, ordered_at, [(part, qty, unit_price)]) — price story per PO.
    ("4500020001", "2026-07-20", [("NW-1001", 50, 27.50), ("NW-1002", 20, 4.20)]),
    ("4500020002", "2026-07-24", [("NW-1003", 6, 95.00)]),            # overpaid
    ("4500020003", "2026-07-27", [("NW-1006", 24, 2.20),
                                  ("NW-8888", 5, 14.00)]),            # off-catalog
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--vendor-email", default="orders@northwind-demo.test")
    ap.add_argument("--vendor-password", default="vendor-demo-2026")
    args = ap.parse_args()

    c = httpx.Client(base_url=args.base.rstrip("/"), timeout=120)

    def ok(r, what):
        if r.status_code >= 400:
            print(f"  ! {what}: {r.status_code} {r.text[:300]}")
            return None
        print(f"  + {what}")
        return r.json() if r.content else {}

    print(f"Seeding {args.tenant} at {args.base}")

    # ---- log in -----------------------------------------------------------
    r = c.post("/api/auth/login", json={"tenant_id": args.tenant,
                                        "email": args.email,
                                        "password": args.password})
    if r.status_code != 200:
        print(f"Login failed: {r.status_code} {r.text[:200]}")
        print("Register the account in the UI first, then re-run.")
        return 1
    auth = {"Authorization": "Bearer " + r.json()["token"]}
    print("  + logged in")

    # ---- supplier + contract ---------------------------------------------
    r = c.post("/api/suppliers", headers=auth, json=SUPPLIER)
    if r.status_code == 409:
        print("  = supplier exists")
    else:
        ok(r, f"supplier {SUPPLIER['code']}")
    supplier = next(
        (s for s in c.get("/api/suppliers", headers=auth).json()
         if s["code"] == SUPPLIER["code"]), None)
    if supplier is None:
        print("Could not find or create the supplier — stopping.")
        return 1

    r = c.post("/api/contracts", headers=auth, json={
        "supplier_code": SUPPLIER["code"], "contract_no": CONTRACT_NO,
        "valid_from": "2026-04-01", "valid_to": "2027-03-31",
        "currency": "USD", "precedence": 10})
    print("  = contract exists" if r.status_code == 409 else f"  + contract {CONTRACT_NO}")

    # ---- catalog: upload, approve everything queued, publish -------------
    existing = c.get(f"/api/catalog/items?tenant_id={args.tenant}").json()
    if not any(i["supplier_part_id"].startswith("NW-") for i in existing):
        r = c.post("/api/catalog/upload", headers=auth,
                   files={"file": ("northwind_catalog.csv", CATALOG_CSV.encode(), "text/csv")},
                   data={"supplier_code": SUPPLIER["code"], "tenant_id": args.tenant})
        up = ok(r, "catalog uploaded")
        if up:
            version_id = up["version_id"]
            queue = c.get(f"/api/catalog/versions/{version_id}/review", headers=auth).json()
            pending = [i for i in queue["items"]
                       if i["state"] in ("needs_review", "manual")]
            for item in pending:
                c.post(f"/api/catalog/items/{item['id']}/decide?decision=approve", headers=auth)
            print(f"  + approved {len(pending)} queued row(s)")
            c.post(f"/api/catalog/versions/{version_id}/confirm-mapping", headers=auth)
            ok(c.post(f"/api/catalog/versions/{version_id}/publish", headers=auth),
               "catalog published")
    else:
        print("  = catalog already published")

    # ---- price tiers ------------------------------------------------------
    r = c.post("/api/pricing/tiers/upload", headers=auth,
               files={"file": ("northwind_tiers.csv", TIERS_CSV.encode(), "text/csv")},
               data={"contract_no": CONTRACT_NO,
                     "valid_from": "2026-04-01", "valid_to": "2027-03-31"})
    ok(r, "price tiers loaded")

    # ---- PO key + push orders (as SAP would) ----------------------------
    key = ok(c.post("/api/tenants/po-key", headers=auth), "PO key generated")
    if key:
        po_key = key["po_key"]
        for po_number, ordered_at, lines in ORDERS:
            payload = {
                "po_number": po_number, "currency": "USD", "ordered_at": ordered_at,
                "supplier": {"code": SUPPLIER["code"]},
                "lines": [{"line_no": (n + 1) * 10, "part": p, "quantity": q,
                           "uom": "EA", "unit_price": up}
                          for n, (p, q, up) in enumerate(lines)],
            }
            ok(c.post(f"/api/v1/po/receive?tenant_id={args.tenant}", json=payload,
                      headers={"X-PO-Key": po_key}), f"PO {po_number} received")

    # ---- vendor account, then acknowledge + ship the first order --------
    inv = ok(c.post(f"/api/suppliers/{supplier['id']}/vendor-org", headers=auth, json={}),
             "vendor invite issued")
    vauth = None
    if inv:
        r = c.post("/api/vendor/register", json={
            "invite_code": inv["invite_code"], "email": args.vendor_email,
            "display_name": "Northwind Order Desk", "password": args.vendor_password})
        if r.status_code == 201:
            print("  + vendor account created")
        else:
            print("  = vendor account exists (reusing)")
        r = c.post("/api/vendor/login", json={"email": args.vendor_email,
                                              "password": args.vendor_password})
        if r.status_code == 200:
            vauth = {"Authorization": "Bearer " + r.json()["token"]}

    if vauth:
        queue = c.get("/api/vendor/orders", headers=vauth).json()
        for o in queue:
            if o["sap_po_number"] == ORDERS[0][0] and o["status"] == "received":
                c.post(f"/api/vendor/orders/{o['id']}/acknowledge", headers=vauth)
                c.post(f"/api/vendor/orders/{o['id']}/ship", headers=vauth,
                       json={"tracking_number": "1Z999AA10123456784", "carrier": "UPS"})
                print("  + vendor acknowledged + shipped one order")
            elif o["sap_po_number"] == ORDERS[1][0] and o["status"] == "received":
                c.post(f"/api/vendor/orders/{o['id']}/acknowledge", headers=vauth)
                print("  + vendor acknowledged a second order")

    # ---- leakage audit ---------------------------------------------------
    r = c.post("/api/audit/upload", headers=auth,
               files={"file": ("po_history.csv", PO_HISTORY_CSV.encode(), "text/csv")})
    job = ok(r, "leakage audit queued")
    if job:
        for _ in range(60):
            j = c.get(f"/api/jobs/{job['job_id']}", headers=auth).json()
            if j["status"] in ("succeeded", "dead"):
                break
            time.sleep(1)
        if j["status"] == "succeeded":
            res = j["result"]
            print(f"  + audit done: ${res['leakage_total']:,.2f} paid above contract "
                  f"({res['leakage_pct_of_spend']:.2f}% of spend), "
                  f"{res['off_catalog_lines']} off-catalog line(s)")
        else:
            print(f"  ! audit failed: {(j.get('error') or '')[-200:]}")

    print("\nDone. Demo scene ready:")
    print(f"  admin   {args.base}/admin")
    print(f"  orders  {args.base}/orders")
    print(f"  vendor  {args.base}/vendor   ({args.vendor_email} / {args.vendor_password})")
    print(f"  shop    {args.base}/api/punchout/oci/start?tenant_id={args.tenant}"
          f"&HOOK_URL={args.base}/api/punchout/oci/mock-hook")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Provision a customer tenant — the operator's phase-2 onboarding command.

One call creates the tenant, its punchout secret, its PO key, and the first
admin invite, then prints everything the customer needs (each credential is
shown exactly once — the server stores only hashes).

    python scripts/provision_tenant.py --base https://interlock-s20b.onrender.com \
        --operator-key <OPERATOR_KEY from the server env> \
        --tenant acme --name "Acme Corp" --admin-email buyer.admin@acme.com

Issue a replacement admin invite (lost link) or rotate a punchout secret:

    python scripts/provision_tenant.py --base ... --operator-key ... \
        --tenant acme --reinvite someone@acme.com
    python scripts/provision_tenant.py --base ... --operator-key ... \
        --tenant acme --rotate-punchout-secret

The OPERATOR_KEY lives in the server's environment (Render dashboard →
interlock → Environment). Without it the /api/ops endpoints are disabled.
"""

from __future__ import annotations

import argparse
import sys

import httpx


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="e.g. http://localhost:8080")
    ap.add_argument("--operator-key", required=True)
    ap.add_argument("--tenant", required=True, help="slug: lowercase letters, digits, dashes")
    ap.add_argument("--name", help="company display name (required when creating)")
    ap.add_argument("--admin-email", help="first admin's email (required when creating)")
    ap.add_argument("--reinvite", metavar="EMAIL", help="issue a fresh admin invite instead of creating")
    ap.add_argument("--rotate-punchout-secret", action="store_true")
    args = ap.parse_args()

    client = httpx.Client(
        base_url=args.base.rstrip("/"),
        headers={"X-Operator-Key": args.operator_key},
        timeout=60,  # free-tier Render can take ~50s to wake
    )

    if args.reinvite:
        r = client.post(f"/api/ops/tenants/{args.tenant}/invites",
                        json={"email": args.reinvite, "role": "admin"})
        _fail_on_error(r)
        d = r.json()
        print(f"Admin invite for {d['email']} (expires {d['expires_at'][:10]}):")
        print(f"  {args.base.rstrip('/')}{d['register_path']}")
        return 0

    if args.rotate_punchout_secret:
        r = client.post(f"/api/ops/tenants/{args.tenant}/punchout-secret")
        _fail_on_error(r)
        d = r.json()
        print(f"New punchout secret for {d['tenant_id']} (shown once):")
        print(f"  {d['punchout_secret']}")
        print(f"  {d['note']}")
        return 0

    if not args.name or not args.admin_email:
        ap.error("--name and --admin-email are required when creating a tenant")

    r = client.post("/api/ops/tenants", json={
        "tenant_id": args.tenant, "name": args.name, "admin_email": args.admin_email,
    })
    _fail_on_error(r)
    d = r.json()
    base = args.base.rstrip("/")
    print(f"Tenant {d['tenant_id']!r} ({d['name']}) provisioned.\n")
    print("== Hand to the customer's ADMIN (their login bootstrap) ==")
    print(f"  Register link (single use, expires {d['admin_invite']['expires_at'][:10]}):")
    print(f"  {base}{d['admin_invite']['register_path']}\n")
    print("== Hand to the customer's BASIS team (SPRO wiring) ==")
    print(f"  Web catalog URL : {base}{d['sap_wiring']['punchout_start']}")
    print(f"  Fixed parameter : PASSWORD = {d['punchout_secret']}")
    print(f"  PO feed         : {base}{d['sap_wiring']['po_receive']}")
    print(f"  X-PO-Key        : {d['po_key']}\n")
    print("Every credential above is shown ONCE. Store them in your password")
    print("manager now; the server keeps only hashes. All are rotatable.")
    return 0


def _fail_on_error(r: httpx.Response) -> None:
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        sys.exit(f"ERROR {r.status_code}: {detail}")


if __name__ == "__main__":
    sys.exit(main())

# Backend

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env
uvicorn app.main:app --reload --port 8080
```

Docs at http://localhost:8080/docs

## Ten-minute walkthrough

```bash
# 1. tenant, supplier, contract
curl -X POST localhost:8080/api/tenants -H 'Content-Type: application/json' \
  -d '{"id":"demo","name":"Demo Manufacturing"}'

curl -X POST localhost:8080/api/suppliers -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo","code":"ACME","name":"Acme Industrial",
       "sap_vendor_no":"0000100234","protocol":"hosted"}'

curl -X POST localhost:8080/api/contracts -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo","supplier_code":"ACME","contract_no":"ACME-FY26",
       "valid_from":"2026-04-01","valid_to":"2027-03-31"}'

# 2. catalog — German headers, mixed units, one broken row
curl -F "file=@samples/catalog_sample.csv" -F "supplier_code=ACME" \
     localhost:8080/api/catalog/upload

# 3. review, approve, publish
curl "localhost:8080/api/catalog/versions/<VERSION_ID>/review"
curl -X POST "localhost:8080/api/catalog/items/<ITEM_ID>/decide?decision=approve&actor=you"
curl -X POST "localhost:8080/api/catalog/versions/<VERSION_ID>/publish"

# 4. price tiers
curl -F "file=@samples/price_tiers_sample.csv" -F "contract_no=ACME-FY26" \
     -F "valid_from=2026-04-01" -F "valid_to=2027-03-31" \
     localhost:8080/api/pricing/tiers/upload

curl "localhost:8080/api/pricing/resolve?part=ACME-4471&quantity=50"

# 5. policy — requests a window wider than the contract, on purpose
curl -F "file=@samples/policy_sample.yaml" localhost:8080/api/policy/upload
curl -X POST "localhost:8080/api/policy/<POLICY_ID>/activate?actor=you"

curl -X POST "localhost:8080/api/policy/evaluate?tenant_id=demo" \
  -H 'Content-Type: application/json' \
  -d '{"line_total":6400,"competitive_quotes":1,"company_code":"1000",
       "material_group":"10203040","is_contracted":true}'
```

## Layout

```
app/
  main.py            App wiring
  config.py          Every setting. All documented in ../START-HERE.md
  db.py              SQLite locally, Postgres in production
  models.py          Catalog, tiers, contracts, policy, punchout sessions
  services/
    ingestion.py     Column mapping, UoM normalization, row validation
    pricing.py       Tier shape parsing, ladder validation, resolution
    policy.py        Contract clamping, deterministic rule evaluation
  api/
    admin.py         Tenants, suppliers, contracts
    catalog.py       Upload, review queue, approve, publish
    pricing.py       Tier upload, price resolution
    policy.py        Upload, preview window, activate, evaluate
    punchout.py      OCI start and return
```

## Before this leaves your laptop

1. `_encrypt` / `_decrypt` in `api/punchout.py` are placeholders. Swap for Fernet
   or KMS — the interface is narrow so it is one commit.
2. Switch `DATABASE_URL` to Postgres and add Alembic migrations.
3. Add authentication. Every endpoint is currently open.
4. `tenant_id` arrives as a request parameter. Derive it from the authenticated
   session instead, and enable Postgres row-level security.

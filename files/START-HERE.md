# START HERE — everything I need from you

> **This is the only file that asks you for anything.**
> Nothing else in this repo requires credentials, keys, or decisions from you.
> Fill in what you can, leave the rest — the platform runs in local mode without any of it.

Work top to bottom. Sections are ordered by when you actually need them.

---

## Section 0 — Run it right now (needs nothing from you)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env
uvicorn app.main:app --reload --port 8080
```

Then open `frontend/index.html` in a browser, and the API docs at http://localhost:8080/docs

Out of the box this uses SQLite and a rule-based classifier. No keys needed. Load the sample catalog:

```bash
curl -F "file=@samples/catalog_sample.csv" -F "supplier_code=ACME" \
     http://localhost:8080/api/catalog/upload
```

Everything below is for making it real.

---

## Section 1 — AI provider (needed for smart ingestion)

Pick one or more. The router falls back gracefully: if no key is set, ingestion uses deterministic
rules only and flags more rows for manual review. It still works, just with lower auto-approve coverage.

| What | Where to get it | Put it in `.env` as |
|---|---|---|
| Anthropic API key | console.anthropic.com → API Keys | `ANTHROPIC_API_KEY=` |
| OpenAI API key *(optional, for embeddings)* | platform.openai.com → API Keys | `OPENAI_API_KEY=` |
| Voyage / Cohere key *(optional, reranking)* | dashboards of either | `RERANK_API_KEY=` |

**Also decide (put in `.env`):**

```
LLM_MODEL_CHEAP=          # bulk classification. Suggest a small/fast model.
LLM_MODEL_SMART=          # column mapping + policy drafting. Frontier tier.
EMBEDDING_MODEL=          # for search + duplicate detection
LLM_BATCH_MODE=true       # 50% cheaper for catalog loads. Leave on.
```

I've left these blank deliberately — model names change every few months and you should
check current pricing when you set this up rather than inherit a stale choice.

---

## Section 2 — Database

Local dev needs nothing. For anything real:

```
DATABASE_URL=postgresql+psycopg://user:password@host:5432/punchout
```

You need Postgres 16 with the `pgvector` extension. Your home server already runs Postgres —
you just need `CREATE EXTENSION vector;` in the target database.

```
PGVECTOR_ENABLED=true
```

---

## Section 3 — Your SAP S/4HANA system

Needed before the first real punchout. Get these from your Basis team.

| Field | What it is | `.env` key |
|---|---|---|
| System ID | e.g. `S4P` | `SAP_SYSTEM_ID=` |
| Client | e.g. `100` | `SAP_CLIENT=` |
| Fiori base URL | where users hit the launchpad | `SAP_FIORI_BASE_URL=` |
| OCI version | `4.0` or `5.0` | `SAP_OCI_VERSION=4.0` |
| Company code | default for requisitions | `SAP_COMPANY_CODE=` |
| Purchasing org | | `SAP_PURCH_ORG=` |
| Default plant | | `SAP_PLANT=` |
| Currency | | `SAP_CURRENCY=USD` |

**And these two, which you'll need to give your Basis team rather than get from them:**

- The **gateway URL** they enter in `SPRO → Materials Management → Purchasing → Environment Data → Web Services`.
  Once deployed, that's `https://<your-domain>/api/punchout/oci/start`
- The **CA certificate chain** for that domain, to import into `STRUST`.
  Use a public CA. Self-signed will cost you two days.

**For OCI 5.0 extraction only** (S/4 pulls catalog JSON from you) — they'll also need:
- Your static egress IPs for their allowlist
- The extraction endpoint: `https://<your-domain>/api/catalog/oci5/extract`

---

## Section 4 — Suppliers you want to connect

For each supplier, one row in the table below. You get these from the supplier's
e-commerce or EDI contact — ask for their "cXML PunchOut credentials."

| Field | Example | Notes |
|---|---|---|
| Supplier name | Acme Industrial | |
| SAP vendor number | `0000100234` | **From your system, not theirs.** This is the field everyone forgets |
| Protocol | `cxml` / `oci` / `hosted` | `hosted` = they just send you a spreadsheet |
| PunchOut endpoint URL | `https://punchout.acme.com/setup` | cXML only |
| From domain / identity | `DUNS` / `123456789` | cXML only |
| To domain / identity | `NetworkID` / `ACME` | cXML only |
| Shared secret | | **cXML only. Never commit this.** See Section 6 |
| Deployment mode | `test` then `production` | Always start on test |

Enter these through the admin API (`POST /api/suppliers`) or the admin UI — **not** into a file.
Secrets are stored encrypted, never in the repo.

---

## Section 5 — Contracts and policy

Before you can activate a procurement policy, you need at least one contract, because
policy validity is clamped to contract validity (that was your requirement, and it's
enforced in code — see `backend/app/services/policy.py`).

**Per contract:**

| Field | Notes |
|---|---|
| Contract number | Your internal reference |
| SAP outline agreement no. | Optional, links to `EKKO` |
| Supplier | Must exist first |
| Valid from / valid to | **Policy windows can never exceed these dates** |
| Currency | |

**Per policy** — either write the rules yourself in YAML (see `samples/policy_sample.yaml`)
or upload your existing procurement policy document and let the policy agent draft the rules
for you to review. It never activates anything on its own; drafted rules land in `draft` status.

---

## Section 6 — Secrets handling

Local dev: `.env` file, already in `.gitignore`.

Anything beyond your laptop:

```
SECRET_BACKEND=env          # local only
SECRET_BACKEND=aws_secrets  # set AWS_REGION + AWS_SECRET_PREFIX
SECRET_BACKEND=vault        # set VAULT_ADDR + VAULT_TOKEN
ENCRYPTION_KEY=             # 32-byte base64. Generate: openssl rand -base64 32
```

`ENCRYPTION_KEY` encrypts supplier shared secrets and stored HOOK_URLs at rest.
**Generate a new one per environment and never reuse it.** If you lose it, every
supplier connection has to be re-credentialed.

---

## Section 7 — Branding for the landing page

Optional. `frontend/index.html` ships with placeholder copy under the name **Interlock**.

- Product name and wordmark
- Contact email for the demo CTA
- Any customer logos you're allowed to show

Change them directly in the HTML — it's a single self-contained file with no build step.

---

## Checklist

- [ ] Section 0 — ran it locally, sample catalog loaded
- [ ] Section 1 — AI key set, model names chosen
- [ ] Section 2 — Postgres with pgvector
- [ ] Section 3 — S/4 details from Basis; gateway URL given to them; cert in `STRUST`
- [ ] Section 4 — first supplier credentialed, on `test` mode
- [ ] Section 5 — first contract loaded, first policy drafted
- [ ] Section 6 — `ENCRYPTION_KEY` generated, secret backend chosen
- [ ] Section 7 — landing page renamed

When Sections 0–5 are done you can run a full punchout round trip against a real supplier.

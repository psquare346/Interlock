# SCALE — build it so it doesn't break later

Companion to [GO-LIVE.md](GO-LIVE.md). GO-LIVE lists the seams to repoint for
*one* customer on the internet. This file is the strategy for the step after:
building the remaining features (PR-create, PO-receipt, vendor network, leakage
audit) in a shape that survives 10 customers × 100 vendors × 100k-line catalogs
without a rewrite.

## The strategy in one paragraph

Keep the app tier **stateless and boring** and push every kind of state into a
service built for it: relational state → Postgres, queued work → a job table
(later Redis), files → an object-storage seam, cross-request coordination →
nothing (never in-process memory). Make every integration **idempotent** so
retries are always safe, because at scale everything retries. Enforce tenancy
**in the database**, not just the code. Then scaling is only ever "run more
copies of the same container" — no architectural event required.

## Decide-now items (cheap this week, brutal after customer data exists)

| # | Decision | Why now | Cost now vs later |
|---|----------|---------|-------------------|
| D1 | **Alembic from the first new table.** Next schema change (global vendor identity) ships as migration 001; `create_all` stays for tests only. | The first migration *after* production data exists is the one that hurts. | half a day now vs a data-surgery weekend later |
| D2 | **Develop against Postgres, keep SQLite for tests.** Run Postgres in Docker locally; CI runs the suite against both. | SQLite hides concurrency bugs, type coercion, and case-sensitivity differences until the worst moment (customer go-live). | a day now vs debugging in production later |
| D3 | **Global vendor identity modeled before the vendor portal is built.** New `vendor_orgs` table + `vendor_org_id` FK on the existing per-tenant `suppliers`; vendor users hang off the org, tenant data stays tenant-scoped. | This is the network model's foundation. Retrofitting a global identity onto per-tenant supplier logins means migrating live vendor accounts. | schema design now vs account-merge migration later |
| D4 | **Idempotency as a rule for every integration endpoint.** PO-receipt: unique on (tenant, SAP PO number, change version) — a re-push updates, never duplicates. PR-create: client-generated idempotency key stored with the submission, so a double-click or our retry never creates two PRs in SAP. Vendor delivery: outbox row per order, retried with backoff until acknowledged, dead-letter after N failures — never fire-and-forget. | SAP output management *will* retry. Networks *will* deliver twice. Duplicate POs at a customer = credibility gone. | a design habit now vs incident postmortems later |
| D5 | **A `jobs` table before any long-running feature.** Ingestion currently parses uploads inside the HTTP request (`api/catalog.py`, `api/pricing.py`) — fine to 10k rows, a timeout at 100k, and the leakage audit (12 months of PO lines) will exceed it on day one. Pattern: upload → store payload → insert job row → return job id → client polls; a worker loop processes jobs. Start with a thread in the same process; the seam lets it move to a real worker container + Redis with zero API change. | The leakage audit — the sales weapon — is the first feature that needs it. Build it on the job seam from day one. | a day now vs rewriting the audit + ingestion under customer pressure later |
| D6 | **Object-storage seam** (`services/storage.py`: `put/get/url`, local-disk backend now, S3 later). Uploaded spreadsheets and generated audit reports go through it — never raw paths, never in-DB blobs. | Keeps the app tier stateless (any container can serve any request), which is the entire horizontal-scaling story. | an hour now vs "which server has the file?" later |
| D7 | **Version every externally-configured URL.** SAP-facing endpoints go live as `/api/v1/…`. Basis teams hard-code URLs into SOAMANAGER/ports; changing them later means change requests at every customer. | Breaking a Basis-configured URL is a multi-customer coordination project. | a route prefix now vs coordinated customer migrations later |

## Phase plan

**Phase 0 — foundations (do while building the next features, ~1 week woven in)**
D1–D7 above, plus a `Dockerfile` + `docker-compose.yml` (app + Postgres) and CI
(GitHub Actions: run the test suite on every push, against SQLite and Postgres).
Exit test: `docker compose up` on a clean machine gives a working platform; the
suite is green on both databases.

**Phase 1 — pilot go-live (one customer, ~1 week, mostly GO-LIVE.md items 1–4)**
Managed Postgres + backups with a *tested restore*; reverse proxy + public-CA
TLS; real `ENCRYPTION_KEY` + secret backend; IP-allowlist on `/api/v1/punchout*`
and integration endpoints; observability floor: structured JSON logs with a
request id and `tenant_id` on every line, Sentry (or equivalent) for tracebacks,
`/healthz` (process up) and `/readyz` (DB reachable) for the proxy, and a daily
job-failure/dead-letter report. Exit test: kill the app container mid-traffic —
proxy fails over to a second replica, no user notices; restore last night's
backup into a scratch DB and run the test suite against it.

**Phase 2 — multi-customer scale (before customer #3)**
Postgres **row-level security** keyed on `tenant_id` (defense-in-depth behind
the app checks — one poisoned query can no longer cross tenants); per-tenant
rate limits and ingestion quotas (noisy-neighbor: one tenant's 500k-row upload
must not starve another's punchout); job workers as separate containers with
Redis as the queue backend (the D5 seam makes this a config change); load test
with a realistic profile — 1M catalog items, 50k-line audit, 20 concurrent
punchout sessions — and fix what it finds *before* a customer finds it.
Exit test: two app replicas + two workers, one tenant hammering ingestion,
another tenant's cart round-trip stays under 500 ms.

**Phase 3 — enterprise asks (when a customer forces it, not before)**
SSO/OIDC (the auth seam already isolates this to one module), status-back-into-
SAP inbound interfaces, per-vendor cXML/EDI adapters, SOC 2 groundwork (the
audit rows + immutable logs from Phase 1 are most of the evidence trail).

## Standing rules while building (the anti-break checklist)

- **No in-process state, ever.** No module-level caches of tenant data, no
  in-memory rate counters, no "temporary" global dicts. If it must be shared,
  it lives in the DB (later Redis). This single rule is what keeps "add a
  replica" a non-event.
- **Every list endpoint paginated from birth.** Retrofitting pagination onto a
  UI that assumes full lists is miserable. Default page size, max page size,
  stable ordering.
- **Every new query gets an index decision.** Catalog search and price
  resolution are per-cart-line hot paths; composite indexes on
  `(tenant_id, …)` leading columns, checked in the migration that adds the table.
- **Integration failures are rows, not logs.** Anything that talks to SAP or a
  vendor writes an attempt row with status; retries and dead-letters are
  queryable. "Did customer X's PO reach vendor Y?" must be answerable with a
  SELECT, not a log grep.
- **The test suite is the scaling contract.** Every seam repoint (SQLite→
  Postgres, thread→worker, disk→S3) is proven by the same green suite. Grow it
  with every feature; it's currently 32 tests and should roughly track endpoint
  count.
- **Measure before optimizing.** No caching layers, no async rewrites, no
  microservices on speculation. The profile from the Phase 2 load test decides
  what gets optimized. A modular monolith on Postgres serves this product to
  hundreds of customers; complexity is added only on evidence.

## What deliberately stays simple

One deployable app (modular monolith), one Postgres, one Redis (eventually).
No Kubernetes, no microservices, no event bus, no multi-region — none of these
are needed below hundreds of tenants, and every one of them taxes the two-person
team every day. The architecture above scales by *replication*, and that is
enough for the next two years of any realistic growth curve.

# GO-LIVE — the repoint strategy

Everything dev-grade in this platform sits behind a deliberate **seam**: a single
module or env var you repoint at hardened infrastructure without touching the
callers. This file lists every seam, what it points at today, what it must point
at for production, and exactly where the change lands.

Work the table top to bottom; items 1–4 are blocking for any internet exposure.

| # | Seam | Today (dev) | Go-live target | Where to repoint |
|---|------|-------------|----------------|------------------|
| 1 | Secret storage | `.env` file, `SECRET_BACKEND=env` | AWS Secrets Manager or Vault | `.env`: `SECRET_BACKEND=aws_secrets` + `AWS_REGION`/`AWS_SECRET_PREFIX` (or `vault` + `VAULT_ADDR`/`VAULT_TOKEN`). Code seam: `backend/app/services/secrets.py` — only `encrypt()`/`decrypt()` exist, so a KMS implementation is one file. |
| 2 | At-rest encryption | Keyed-XOR fallback (obfuscation) when `ENCRYPTION_KEY` unset | Fernet (already implemented) or KMS envelope | Generate per environment: `openssl rand -base64 32` → `ENCRYPTION_KEY`. Install `cryptography` and the existing code auto-upgrades to Fernet. **Never reuse a key across environments; losing it means re-credentialing every supplier.** |
| 3 | Database | SQLite file `punchout.db`, `create_all` | Postgres 16 + pgvector, Alembic migrations | `.env`: `DATABASE_URL=postgresql+psycopg://…`, `PGVECTOR_ENABLED=true`. Add Alembic before the first prod schema change (`backend/app/db.py` is the only place that touches engine/DDL). Then enable Postgres **row-level security** keyed on `tenant_id` as defense-in-depth behind the app-level tenant checks. |
| 4 | TLS + domain | Plain http://localhost:8080 | Reverse proxy (nginx/Caddy) with a **public-CA** cert | Update CORS origins in `backend/app/main.py` to the real domain. Give SAP Basis the gateway URL `https://<domain>/api/punchout/oci/start` and the CA chain for `STRUST` (self-signed costs you two days — START-HERE §3). |
| 5 | Password hashing | PBKDF2-HMAC-SHA256, 600k iters (fine) | argon2id (better), or delete entirely for SSO | Single seam: `_hash_password()` in `backend/app/services/auth.py`. Old hashes keep working if you version the hash prefix. For enterprise: replace register/login with OIDC (Azure AD/Okta) — `get_current_user()` is the only function the rest of the app calls, so SSO is a swap inside one module. |
| 6 | Session tokens | Hashed opaque tokens in SQLite, 12 h TTL | Same pattern in Postgres is acceptable; Redis if you need instant global revocation at scale | Storage is touched only by `login()/logout()/get_current_user()` in `services/auth.py`. TTL/lockout knobs are constants at the top of that file. |
| 7 | Rate limiting | Login lockout only (5 fails / 15 min) | Proxy-level rate limits on `/api/auth/*` and `/api/punchout/*` | Do it at the reverse proxy (item 4), not in app code. |
| 8 | Punchout exposure | **Credentialed**: `/oci/start` requires the per-tenant punchout secret (`PASSWORD` param from SPRO); storefront items need an open session or staff login; no secret configured = punchout closed | Keep the credential, add network-layer restriction on top | Secrets are issued at provisioning (`/api/ops/tenants`) and rotatable by tenant admins (`/api/tenants/punchout-secret`). Still IP-allowlist SAP egress IPs at the proxy as defense-in-depth. Session hooks are already encrypted at rest and single-use. |
| 9 | AI provider keys | Blank → rule-only mode | Keys in the secret backend, never in `.env` on a server | `.env` keys move to item 1's backend. Spend guard already exists: `LLM_MAX_SPEND_USD_PER_JOB` in `backend/app/config.py`. Keep `LLM_BATCH_MODE=true` (50% cheaper). |
| 10 | Admin UI origin | Served same-origin at `/admin`, CORS locked to localhost | Same pattern — just add the prod domain to CORS in `main.py` | Never re-enable `allow_origins=["*"]`; the UI being same-origin is what makes the lock possible. |
| 11 | Audit | `decided_by`/`activated_by`/`PolicyEvaluation` rows from authenticated identity | Ship logs somewhere immutable | Add request logging at the proxy; DB audit columns already capture who did what. |
| 12 | Backups / key rotation | none | Nightly Postgres backups; rotate `ENCRYPTION_KEY` and API keys on a schedule | Re-encrypt supplier secrets on rotation: decrypt-with-old, encrypt-with-new — the `dev$`/`fer$` prefix scheme in `services/secrets.py` already supports two live schemes side by side. |

## The one-page runbook

1. Provision Postgres 16 (+`CREATE EXTENSION vector;`), point `DATABASE_URL` at it, run Alembic.
2. Stand up the reverse proxy with a public-CA cert; set CORS to the real domain.
3. Move every secret to the secret backend; generate a fresh `ENCRYPTION_KEY`; install `cryptography`.
4. Set `OPERATOR_KEY` and provision the tenant (`scripts/provision_tenant.py`) — this mints the punchout secret, PO key, and the first admin's invite in one step. Registration is invite-only; there is no open signup to race.
5. IP-allowlist `/api/punchout/*`; hand Basis the gateway URL + CA chain; import into `STRUST`.
6. Onboard supplier #1 in `deployment_mode=test`; run a full round trip; only then flip to `production`.
7. Load contract → catalog → tiers → policy (that order matters: policies clamp to contracts; tiers validate against contracts).

## What never changes at go-live

The business core is environment-independent on purpose: ingestion validation,
ladder validation, contract clamping, deterministic policy evaluation, and price
resolution (`backend/app/services/`) have no idea where secrets, sessions, or
the database live. Hardening is repointing seams — not rewriting logic — and the
32-test suite is the proof after each repoint.

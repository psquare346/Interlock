"""App wiring."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .config import get_settings
from .db import SessionLocal, init_db
from .api import (
    admin, audit, auth, catalog, jobs, orders, po, policy, pricing, punchout, vendor,
)
from .services import audit as _audit_handlers  # noqa: F401 — registers job handlers
from .services.jobs import Worker

FRONTEND = Path(__file__).resolve().parent.parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings = get_settings()
    worker = Worker(SessionLocal, poll_seconds=settings.JOB_POLL_SECONDS)
    if settings.RUN_WORKER:
        worker.start()
    yield
    worker.stop()


app = FastAPI(
    title="Interlock",
    description="Punchout catalog gateway for SAP S/4HANA: OCI to SAP, cXML to suppliers, "
    "AI-assisted catalog ingestion with human-gated review.",
    version="0.2.0",
    lifespan=lifespan,
)

# The admin UI is served same-origin (below), so CORS only needs to admit
# local dev origins. file:// pages are NOT allowed — use /admin instead.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://127.0.0.1:8080"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(admin.router, prefix="/api", tags=["admin"])
app.include_router(catalog.router, prefix="/api/catalog", tags=["catalog"])
app.include_router(pricing.router, prefix="/api/pricing", tags=["pricing"])
app.include_router(policy.router, prefix="/api/policy", tags=["policy"])
app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
app.include_router(orders.router, prefix="/api/orders", tags=["orders"])
app.include_router(audit.router, prefix="/api/audit", tags=["audit"])
app.include_router(vendor.router, prefix="/api/vendor", tags=["vendor"])
# Punchout stays token-free by design: SAP and supplier systems call it, not
# logged-in admin users. Production protects it with TLS + IP allowlisting.
app.include_router(punchout.router, prefix="/api/punchout", tags=["punchout"])
# Versioned aliases for every URL that gets hard-coded into SAP configuration
# (SCALE.md D7). Basis teams configure /api/v1/...; breaking it later means a
# change request at every customer, so it never changes shape.
app.include_router(punchout.router, prefix="/api/v1/punchout", include_in_schema=False)
app.include_router(po.router, prefix="/api/v1/po", tags=["po"])


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def landing():
    return FileResponse(FRONTEND / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_ui():
    return FileResponse(FRONTEND / "admin.html")


@app.get("/orders", include_in_schema=False)
def orders_ui():
    return FileResponse(FRONTEND / "orders.html")


@app.get("/vendor", include_in_schema=False)
def vendor_ui():
    return FileResponse(FRONTEND / "vendor.html")

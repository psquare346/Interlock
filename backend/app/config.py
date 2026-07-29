"""Every setting the platform reads. All documented in ../START-HERE.md.

Everything has a local-mode default: no key, no Postgres, no SAP system is
required to run. Missing AI keys degrade to rule-only ingestion; a missing
ENCRYPTION_KEY falls back to a dev-only obfuscation cipher.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database (START-HERE §2) ---
    DATABASE_URL: str = "sqlite:///./punchout.db"
    PGVECTOR_ENABLED: bool = False

    # --- Storage & background jobs (SCALE.md D5/D6) ---
    STORAGE_DIR: str = "./data"
    RUN_WORKER: bool = True          # in-process worker; off when real workers exist
    JOB_POLL_SECONDS: float = 1.0

    # --- AI provider (START-HERE §1) ---
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    RERANK_API_KEY: str = ""
    LLM_MODEL_CHEAP: str = ""
    LLM_MODEL_SMART: str = ""
    EMBEDDING_MODEL: str = ""
    LLM_BATCH_MODE: bool = True
    LLM_MAX_SPEND_USD_PER_JOB: float = 10.0

    # --- Ingestion gates (catalog-ingestion-agent.md) ---
    AUTO_APPROVE_THRESHOLD: float = 0.95
    REVIEW_THRESHOLD: float = 0.70
    PRICE_CHANGE_FLAG_PCT: float = 10.0

    # --- SAP S/4HANA (START-HERE §3) ---
    SAP_SYSTEM_ID: str = ""
    SAP_CLIENT: str = ""
    SAP_FIORI_BASE_URL: str = ""
    SAP_OCI_VERSION: str = "4.0"
    SAP_COMPANY_CODE: str = ""
    SAP_PURCH_ORG: str = ""
    SAP_PLANT: str = ""
    SAP_CURRENCY: str = "USD"

    # --- Secrets (START-HERE §6) ---
    SECRET_BACKEND: str = "env"
    ENCRYPTION_KEY: str = ""
    AWS_REGION: str = ""
    AWS_SECRET_PREFIX: str = ""
    VAULT_ADDR: str = ""
    VAULT_TOKEN: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()

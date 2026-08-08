"""Settings loaded from the environment / .env.

Contract: single source of configuration for every process (ingest, worker,
recon, harness). Secrets come only from the environment; a `STRIPE_SECRET_KEY`
that does not start with `sk_test_` is a hard startup error — this project is
test mode only, by rule.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    stripe_secret_key: str = ""  # STRIPE_SECRET_KEY
    stripe_webhook_secret: str = ""  # STRIPE_WEBHOOK_SECRET
    database_url: str = "postgresql://postgres:ledgerproof@localhost:5432/ledgerproof"
    test_database_url: str = "postgresql://postgres:ledgerproof@localhost:5432/ledgerproof_test"
    app_database_url: str = "postgresql://ledger_app:ledger_app@localhost:5432/ledgerproof"
    redis_url: str = "redis://localhost:6379/0"
    # Phase 5 only. Read through Settings rather than letting the Anthropic SDK
    # pick it out of the environment: python-dotenv resolves .env relative to
    # the CALLING FILE, so a script outside the repo root silently gets no key
    # and fails at request time instead of at startup.
    anthropic_api_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("stripe_secret_key")
    @classmethod
    def _test_mode_only(cls, v: str) -> str:
        if not v or v.startswith("sk_test_"):
            return v
        if v.startswith("sk_live_"):
            raise ValueError(
                "REFUSING TO START: STRIPE_SECRET_KEY is a LIVE-MODE key (sk_live_...). "
                "LedgerProof is test mode only, by rule — it deliberately duplicates, "
                "tampers with, and replays payment traffic. Replace the key with a "
                "sandbox/test-mode key (sk_test_...) and never commit a live key anywhere."
            )
        raise ValueError(
            "STRIPE_SECRET_KEY must be a test-mode key starting with 'sk_test_' "
            "(or empty when Stripe access is not needed)."
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

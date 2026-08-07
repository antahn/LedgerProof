"""Fixtures for test-clock suites: live sandbox + a dedicated ledger database.

These tests talk to the real Stripe sandbox, so they skip cleanly when no
sk_test_ key is configured (CI without secrets must not report a false pass).
Every clock is deleted in teardown, which deletes the objects on it.
"""

from __future__ import annotations

import importlib.util
import os
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
import stripe

from ledgerproof.config import get_settings
from tests.clocks import clockkit

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOCK_DB = "ledgerproof_clocks"
ADMIN_URL = "postgresql://postgres:ledgerproof@localhost:5432/postgres"
CLOCK_DB_URL = f"postgresql://postgres:ledgerproof@localhost:5432/{CLOCK_DB}"

# Fixed, far-past-proof starting point. Forward-only means every run needs a
# clock created fresh at this instant; it never advances between runs.
FROZEN_START = 1_780_000_000


def _load_migrate():
    spec = importlib.util.spec_from_file_location("_migrate", REPO_ROOT / "scripts" / "migrate.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.migrate


@pytest.fixture(scope="session")
def stripe_test_mode() -> str:
    key = get_settings().stripe_secret_key
    if not key:
        pytest.skip("STRIPE_SECRET_KEY not set: test-clock suites need a live sandbox")
    assert key.startswith("sk_test_"), "test clocks are sandbox-only"
    stripe.api_key = key
    return key


@pytest.fixture()
def clock_db() -> Iterator[str]:
    """A freshly migrated ledger database, isolated from dev and chaos runs."""
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{CLOCK_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{CLOCK_DB}"')
    _load_migrate()(CLOCK_DB_URL)
    yield CLOCK_DB_URL


@pytest.fixture()
def session(stripe_test_mode: str, clock_db: str, request) -> Iterator[clockkit.ClockSession]:
    """A test clock with a customer on it; deleted (with its objects) on teardown."""
    payment_method = getattr(request, "param", "pm_card_visa")
    started_at = int(time.time())
    clock_id = clockkit.create_clock(FROZEN_START, name=f"ledgerproof-{request.node.name}"[:60])
    customer_id, pm_id = clockkit.create_customer_on_clock(clock_id, payment_method)
    sess = clockkit.ClockSession(
        clock_id=clock_id,
        customer_id=customer_id,
        db_url=clock_db,
        started_at=started_at,
    )
    sess.track(customer_id, pm_id)
    try:
        yield sess
    finally:
        clockkit.delete_clock(clock_id)


@pytest.fixture(scope="session")
def clock_metrics() -> Iterator[dict]:
    """Collects wall-clock and simulated-time totals for the artifact."""
    metrics: dict = {"suites": [], "started_at": time.time()}
    yield metrics
    metrics["wall_clock_s"] = round(time.time() - metrics["started_at"], 1)
    out = REPO_ROOT / "artifacts" / "clocks_run.json"
    if metrics["suites"]:
        import json

        payload = {
            "wall_clock_s": metrics["wall_clock_s"],
            "simulated_span_days": round(
                sum(s["simulated_span_s"] for s in metrics["suites"]) / 86400, 1
            ),
            "suites": metrics["suites"],
            "recorded_at": os.environ.get("LEDGERPROOF_RUN_STAMP", ""),
        }
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

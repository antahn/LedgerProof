"""The chaos harness's lifecycle manager, exercised against REAL processes.

These tests start uvicorn and a celery worker, hard-kill them, and drive a
signed webhook end to end, because the properties under test (a hard kill
leaves no orphan, quiescence is real and never hangs) do not exist in a mock.
They are marked `chaos` so they can be deselected: `-m "not chaos"`.

The stack is module-scoped and pinned to port 8100: one startup for the whole
file, and no two Stacks ever race for the port. Tests that count rows reset the
ledger first, so each is independently runnable.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    # harness/ is not an installed package (the wheel ships src/ledgerproof
    # only), and pytest puts the test's own directory on sys.path, not rootdir.
    sys.path.insert(0, str(REPO_ROOT))

from harness.stack import Stack
from ledgerproof.ledger import invariant
from ledgerproof.ledger.balances import all_balances
from ledgerproof.stripe_io.signature import sign

pytestmark = pytest.mark.chaos


def _charge_succeeded(*, amount: int = 1000, fee: int = 59) -> dict:
    """A charge.succeeded envelope with an EXPANDED balance_transaction.

    Built inline rather than imported from harness/events.py: this file must
    not depend on a module being written concurrently. The expansion is what
    lets the worker post without any Stripe egress (the stack runs with
    STRIPE_SECRET_KEY empty).
    """
    suffix = uuid.uuid4().hex[:12]
    return {
        "id": f"evt_{suffix}",
        "object": "event",
        "type": "charge.succeeded",
        "created": int(time.time()),
        "data": {
            "object": {
                "id": f"ch_{suffix}",
                "object": "charge",
                "amount": amount,
                "currency": "usd",
                "balance_transaction": {
                    "id": f"txn_{suffix}",
                    "object": "balance_transaction",
                    "amount": amount,
                    "fee": fee,
                    "net": amount - fee,
                },
            }
        },
    }


def _deliver(stack: Stack, event: dict, *, timeout: float = 10.0) -> httpx.Response:
    body = json.dumps(event, separators=(",", ":")).encode()
    return httpx.post(
        stack.ingest_url,
        content=body,
        headers={
            "Stripe-Signature": sign(body, stack.webhook_secret),
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )


def _count(db_url: str, table: str) -> int:
    with psycopg.connect(db_url) as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    running = Stack()
    running.create_database()
    with running:
        yield running


def test_create_database_and_reset_ledger_produce_a_migrated_empty_ledger() -> None:
    fresh = Stack()
    fresh.create_database()

    for phase in ("created", "reset"):
        if phase == "reset":
            fresh.reset_ledger()
        with psycopg.connect(fresh.db_url) as conn:
            accounts = conn.execute("SELECT count(*) FROM accounts").fetchone()
            txns = conn.execute("SELECT count(*) FROM transactions").fetchone()
            events = conn.execute("SELECT count(*) FROM stripe_events").fetchone()
        assert accounts is not None and accounts[0] == 7, phase  # seeded chart of accounts
        assert txns is not None and txns[0] == 0, phase
        assert events is not None and events[0] == 0, phase
        assert invariant.check(fresh.db_url).ok, phase

    # The append-only trigger survives the reset — which is also why the reset
    # is a schema drop and not a TRUNCATE.
    with (
        psycopg.connect(fresh.db_url) as conn,
        pytest.raises(psycopg.errors.RaiseException, match="append-only"),
    ):
        conn.execute("TRUNCATE entries")


def test_start_brings_up_ingest_and_worker(stack: Stack) -> None:
    assert stack.ingest_healthy()
    assert stack.ingest_proc is not None and stack.ingest_proc.poll() is None
    assert stack.worker_proc is not None and stack.worker_proc.poll() is None
    assert "ready" in stack.worker_log(200).lower()
    assert stack.redis_url.endswith("/1")  # never the dev queue


def test_signed_delivery_posts_exactly_one_transaction(stack: Stack) -> None:
    stack.reset_ledger()
    event = _charge_succeeded(amount=1000, fee=59)

    resp = _deliver(stack, event)

    assert resp.status_code == 200
    assert resp.json() == {"status": "queued"}
    assert stack.wait_quiescent(timeout=45.0)
    assert _count(stack.db_url, "transactions") == 1
    assert all_balances(stack.db_url) == {
        "stripe_balance": 941,
        "processing_fees": 59,
        "revenue": 1000,
        "bank": 0,
        "dispute_losses": 0,
        "refunds_contra": 0,
        "customer_liability": 0,
    }
    assert invariant.check(stack.db_url).ok


def test_kill_worker_then_restart_processes_a_fresh_event(stack: Stack) -> None:
    stack.reset_ledger()
    killed = stack.worker_proc
    assert killed is not None

    stack.kill_worker()
    assert killed.poll() is not None  # TerminateProcess, not a graceful stop
    stack.restart_worker()

    assert stack.worker_proc is not None and stack.worker_proc.poll() is None
    resp = _deliver(stack, _charge_succeeded(amount=2500, fee=103))
    assert resp.status_code == 200
    assert stack.wait_quiescent(timeout=45.0)
    assert _count(stack.db_url, "transactions") == 1
    assert all_balances(stack.db_url)["revenue"] == 2500
    assert invariant.check(stack.db_url).ok


def test_wait_quiescent_times_out_when_the_worker_is_down(stack: Stack) -> None:
    stack.reset_ledger()
    stack.kill_worker()

    resp = _deliver(stack, _charge_succeeded())
    assert resp.status_code == 200  # ingest is healthy; only the consumer is gone

    started = time.monotonic()
    quiet = stack.wait_quiescent(timeout=3.0)
    elapsed = time.monotonic() - started

    # False, not a hang — and nothing drained the queue, which also proves the
    # tree kill left no orphan worker behind.
    assert quiet is False
    assert elapsed < 15.0
    assert _count(stack.db_url, "transactions") == 0

    stack.restart_worker()  # the queued event is still there; drain it
    assert stack.wait_quiescent(timeout=45.0)
    assert _count(stack.db_url, "transactions") == 1
    assert invariant.check(stack.db_url).ok


def test_stop_leaves_no_live_processes(stack: Stack) -> None:
    ingest, worker = stack.ingest_proc, stack.worker_proc
    assert ingest is not None and worker is not None

    stack.stop()

    assert ingest.poll() is not None
    assert worker.poll() is not None
    assert not stack.ingest_healthy()  # an orphan uvicorn would still answer
    stack.stop()  # idempotent

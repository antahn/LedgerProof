"""post_transaction: outcomes, dedupe idempotency, retry-with-jitter, and the
balance view's sign logic (direction x account normal)."""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg
import pytest

from ledgerproof.ledger.balances import all_balances, balance
from ledgerproof.ledger.post import Entry, LedgerTransaction, PostOutcome, post_transaction


def _txn(
    entries: tuple[Entry, ...],
    *,
    event_id: str | None = None,
    object_id: str | None = None,
    event_type: str | None = None,
) -> LedgerTransaction:
    return LedgerTransaction(
        id=uuid.uuid4(),
        occurred_at=datetime.now(timezone.utc),
        entries=entries,
        stripe_event_id=event_id,
        stripe_object_id=object_id,
        event_type=event_type,
    )


_CHARGE_ENTRIES = (
    Entry("stripe_balance", "debit", 970),
    Entry("processing_fees", "debit", 30),
    Entry("revenue", "credit", 1000),
)


def _count(db_url: str, table: str) -> int:
    with psycopg.connect(db_url) as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608


def test_posted_on_success(ledger_db: str) -> None:
    outcome = post_transaction(ledger_db, _txn(_CHARGE_ENTRIES))
    assert outcome is PostOutcome.POSTED
    assert _count(ledger_db, "transactions") == 1
    assert _count(ledger_db, "entries") == 3


def test_balance_view_sign_logic(ledger_db: str) -> None:
    # DR stripe_balance 970 + DR processing_fees 30 / CR revenue 1000.
    assert post_transaction(ledger_db, _txn(_CHARGE_ENTRIES)) is PostOutcome.POSTED

    # Debit-normal accounts go positive from debits...
    assert balance(ledger_db, "stripe_balance") == 970
    assert balance(ledger_db, "processing_fees") == 30
    # ...and the credit-normal account goes positive from credits.
    assert balance(ledger_db, "revenue") == 1000

    # A credit to a debit-normal account subtracts: refund of 200.
    refund = _txn((Entry("refunds_contra", "debit", 200), Entry("stripe_balance", "credit", 200)))
    assert post_transaction(ledger_db, refund) is PostOutcome.POSTED
    assert balance(ledger_db, "stripe_balance") == 970 - 200
    assert balance(ledger_db, "refunds_contra") == 200
    assert balance(ledger_db, "revenue") == 1000  # untouched

    balances = all_balances(ledger_db)
    assert balances["stripe_balance"] == 770
    assert balances["bank"] == 0  # untouched account present, zero
    assert len(balances) == 7


def test_duplicate_on_same_stripe_event_id(ledger_db: str) -> None:
    first = _txn(_CHARGE_ENTRIES, event_id="evt_1", object_id="ch_1", event_type="charge.succeeded")
    assert post_transaction(ledger_db, first) is PostOutcome.POSTED

    # Replay: fresh transaction id, same stripe_event_id.
    replay = _txn(_CHARGE_ENTRIES, event_id="evt_1", object_id="ch_1", event_type="charge.succeeded")
    assert post_transaction(ledger_db, replay) is PostOutcome.DUPLICATE
    assert _count(ledger_db, "transactions") == 1
    assert _count(ledger_db, "entries") == 3


def test_duplicate_on_same_event_type_and_object_id(ledger_db: str) -> None:
    first = _txn(_CHARGE_ENTRIES, event_id="evt_1", object_id="ch_1", event_type="charge.succeeded")
    assert post_transaction(ledger_db, first) is PostOutcome.POSTED

    # Stripe's documented gotcha: a DISTINCT event id for the same state change.
    second = _txn(
        _CHARGE_ENTRIES, event_id="evt_2", object_id="ch_1", event_type="charge.succeeded"
    )
    assert post_transaction(ledger_db, second) is PostOutcome.DUPLICATE
    assert _count(ledger_db, "transactions") == 1


def test_unknown_account_raises_value_error(ledger_db: str) -> None:
    bad = _txn((Entry("slush_fund", "debit", 100), Entry("revenue", "credit", 100)))
    with pytest.raises(ValueError, match="slush_fund"):
        post_transaction(ledger_db, bad)
    assert _count(ledger_db, "transactions") == 0


class _FlakyCommitConn:
    """Delegates to a real connection but fails the first N commits with 40001."""

    def __init__(self, real: psycopg.Connection, state: dict[str, int]) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_state", state)

    def commit(self) -> None:
        if self._state["failures_left"] > 0:
            self._state["failures_left"] -= 1
            self._real.rollback()
            raise psycopg.errors.SerializationFailure("injected serialization failure (40001)")
        self._real.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._real, name, value)


def _install_flaky_connect(
    monkeypatch: pytest.MonkeyPatch, failures: int
) -> tuple[dict[str, int], Any]:
    real_connect = psycopg.connect
    state = {"failures_left": failures, "connects": 0}

    def flaky_connect(url: str, *args: Any, **kwargs: Any) -> _FlakyCommitConn:
        state["connects"] += 1
        return _FlakyCommitConn(real_connect(url, *args, **kwargs), state)

    monkeypatch.setattr(psycopg, "connect", flaky_connect)
    return state, real_connect


def test_serialization_failure_retried_with_jittered_backoff(
    ledger_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, real_connect = _install_flaky_connect(monkeypatch, failures=2)
    sleeps: list[float] = []

    outcome = post_transaction(
        ledger_db, _txn(_CHARGE_ENTRIES), sleep=sleeps.append, rng=random.Random(42)
    )

    assert outcome is PostOutcome.POSTED
    assert state["connects"] == 3  # two failed attempts + the success
    assert len(sleeps) == 2  # slept between attempts only
    assert all(d > 0 for d in sleeps)
    assert sleeps[0] != sleeps[1]  # jitter: delays are not identical
    # Exponential base doubles; jitter factor is in [0.5, 1.5), so even the
    # most extreme draws keep the second delay above the first's minimum.
    assert sleeps[1] > sleeps[0] / 3

    with real_connect(ledger_db) as conn:
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1


def test_serialization_failure_gives_up_after_max_attempts(
    ledger_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, real_connect = _install_flaky_connect(monkeypatch, failures=99)
    sleeps: list[float] = []

    with pytest.raises(psycopg.errors.SerializationFailure):
        post_transaction(
            ledger_db,
            _txn(_CHARGE_ENTRIES),
            max_attempts=3,
            sleep=sleeps.append,
            rng=random.Random(7),
        )

    assert state["connects"] == 3  # bounded: exactly max_attempts
    assert len(sleeps) == 2
    with real_connect(ledger_db) as conn:
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0

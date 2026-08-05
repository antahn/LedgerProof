"""post_transaction: outcomes, dedupe idempotency, retry-with-jitter, and the
balance view's sign logic (direction x account normal)."""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime
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
        occurred_at=datetime.now(UTC),
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
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


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
    """Retry mechanics AND real jitter: the same forced failure sequence with
    different rngs must produce different delays — a deterministic backoff
    (which also yields two distinct, growing delays) must fail this test."""
    real_connect = psycopg.connect

    def run(seed: int) -> list[float]:
        # Fresh patch per run so every run sees the SAME forced 40001 sequence.
        with monkeypatch.context() as mp:
            state, _ = _install_flaky_connect(mp, failures=2)
            sleeps: list[float] = []
            outcome = post_transaction(
                ledger_db, _txn(_CHARGE_ENTRIES), sleep=sleeps.append, rng=random.Random(seed)
            )
        assert outcome is PostOutcome.POSTED
        assert state["connects"] == 3  # two failed attempts + the success
        assert len(sleeps) == 2  # slept between attempts only
        return sleeps

    seed1 = run(1)
    seed2 = run(2)

    # Jitter is real: different rngs, same failures, different delay sequences.
    assert seed1 != seed2
    # ...and reproducible: the same seed replays the same sequence.
    assert run(1) == seed1

    # Every delay stays inside the documented envelope:
    # min(0.05 * 2**(attempt-1), 2.0) * jitter, with jitter in [0.5, 1.5).
    for sleeps in (seed1, seed2):
        for attempt, delay in enumerate(sleeps, start=1):
            base = min(0.05 * 2 ** (attempt - 1), 2.0)
            assert base * 0.5 <= delay < base * 1.5

    with real_connect(ledger_db) as conn:
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 3


class _IsolationRecordingConn:
    """Delegates to a real connection; records isolation_level at commit time."""

    def __init__(self, real: psycopg.Connection, recorded: list[Any]) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_recorded", recorded)

    def commit(self) -> None:
        self._recorded.append(self._real.isolation_level)
        self._real.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._real, name, value)


def test_post_transaction_commits_at_serializable(
    ledger_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SERIALIZABLE is actually in effect on the REAL post path at the moment
    of commit. If the isolation_level assignment in _attempt is deleted, the
    recorded value is None (server default), and this test fails."""
    real_connect = psycopg.connect
    recorded: list[Any] = []

    def recording_connect(url: str, *args: Any, **kwargs: Any) -> _IsolationRecordingConn:
        return _IsolationRecordingConn(real_connect(url, *args, **kwargs), recorded)

    monkeypatch.setattr(psycopg, "connect", recording_connect)

    outcome = post_transaction(ledger_db, _txn(_CHARGE_ENTRIES))

    assert outcome is PostOutcome.POSTED
    assert recorded == [psycopg.IsolationLevel.SERIALIZABLE]
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

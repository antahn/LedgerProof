"""Reconciler tests: does the external comparison actually catch what tests miss?

Own scratch database (ledgerproof_test_recon), schema reset per test, so these
never collide with the other integration suites.

The expectations here are duck-typed (`_Expected`), not imported from harness/:
src/ must never depend on its adversary, and this suite proves the Protocol is
satisfiable by anything with .event_id and .expected_delta.
"""

from __future__ import annotations

import importlib.util
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from ledgerproof.ledger.post import Entry, LedgerTransaction, post_transaction
from ledgerproof.recon.breaks import BreakKind
from ledgerproof.recon.reconciler import reconcile_expected, reconcile_stripe

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = "postgresql://postgres:ledgerproof@localhost:5432/postgres"
SCRATCH_DB = "ledgerproof_test_recon"
SCRATCH_URL = f"postgresql://postgres:ledgerproof@localhost:5432/{SCRATCH_DB}"

OCCURRED_AT = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
OCCURRED_TS = int(OCCURRED_AT.timestamp())


def _load_migrate():
    """Import scripts/migrate.py's migrate() by path (scripts/ is not a package)."""
    path = REPO_ROOT / "scripts" / "migrate.py"
    spec = importlib.util.spec_from_file_location("_ledgerproof_migrate_recon", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.migrate


_migrate = _load_migrate()


@pytest.fixture(scope="session")
def _recon_database() -> str:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {SCRATCH_DB}")
    return SCRATCH_URL


@pytest.fixture()
def recon_db(_recon_database: str) -> str:
    """Freshly migrated scratch database; the ledger is append-only, so the
    sanctioned reset is dropping the schema, never DELETE/TRUNCATE."""
    with psycopg.connect(_recon_database, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    _migrate(_recon_database)
    return _recon_database


@dataclass(frozen=True)
class _Expected:
    """Structurally identical to harness.events.EventFixture's recon surface."""

    event_id: str
    expected_delta: dict[str, int] = field(default_factory=dict)


class _StubStripe:
    """Anything with .get(path, params) -> dict is a reconcile_stripe source."""

    def __init__(self, *pages: dict[str, Any]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((path, dict(params or {})))
        return self.pages[len(self.calls) - 1]


def _page(*bts: dict[str, Any], has_more: bool = False) -> dict[str, Any]:
    return {"object": "list", "data": list(bts), "has_more": has_more}


def _bt(bt_id: str, source: str, amount: int, fee: int, bt_type: str = "charge") -> dict[str, Any]:
    return {
        "id": bt_id,
        "object": "balance_transaction",
        "type": bt_type,
        "source": source,
        "amount": amount,
        "fee": fee,
        "net": amount - fee,
        "currency": "usd",
    }


def _txn(
    event_id: str,
    object_id: str,
    event_type: str,
    entries: tuple[Entry, ...],
    *,
    occurred_at: datetime = OCCURRED_AT,
) -> LedgerTransaction:
    return LedgerTransaction(
        id=uuid.uuid5(uuid.NAMESPACE_URL, "ledgerproof:" + event_id),
        occurred_at=occurred_at,
        entries=entries,
        stripe_event_id=event_id,
        stripe_object_id=object_id,
        event_type=event_type,
    )


def _charge(
    event_id: str, charge_id: str, *, gross: int = 1000, fee: int = 59
) -> tuple[LedgerTransaction, _Expected]:
    txn = _txn(
        event_id,
        charge_id,
        "charge.succeeded",
        (
            Entry("stripe_balance", "debit", gross - fee),
            Entry("processing_fees", "debit", fee),
            Entry("revenue", "credit", gross),
        ),
    )
    expected = _Expected(
        event_id,
        {"stripe_balance": gross - fee, "processing_fees": fee, "revenue": gross},
    )
    return txn, expected


def _refund(
    event_id: str, refund_id: str, *, amount: int = 400
) -> tuple[LedgerTransaction, _Expected]:
    txn = _txn(
        event_id,
        refund_id,
        "charge.refunded",
        (
            Entry("refunds_contra", "debit", amount),
            Entry("stripe_balance", "credit", amount),
        ),
    )
    return txn, _Expected(event_id, {"refunds_contra": amount, "stripe_balance": -amount})


def _payout(
    event_id: str, payout_id: str, *, amount: int = 5000
) -> tuple[LedgerTransaction, _Expected]:
    txn = _txn(
        event_id,
        payout_id,
        "payout.paid",
        (
            Entry("bank", "debit", amount),
            Entry("stripe_balance", "credit", amount),
        ),
    )
    return txn, _Expected(event_id, {"bank": amount, "stripe_balance": -amount})


def _dispute(
    event_id: str, dispute_id: str, *, amount: int = 2000, fee: int = 1500
) -> tuple[LedgerTransaction, _Expected]:
    txn = _txn(
        event_id,
        dispute_id,
        "charge.dispute.created",
        (
            Entry("dispute_losses", "debit", amount),
            Entry("processing_fees", "debit", fee),
            Entry("stripe_balance", "credit", amount + fee),
        ),
    )
    expected = _Expected(
        event_id,
        {"dispute_losses": amount, "processing_fees": fee, "stripe_balance": -(amount + fee)},
    )
    return txn, expected


def _kinds(result: Any) -> list[BreakKind]:
    return [b.kind for b in result.breaks]


# --------------------------------------------------------------------------
# reconcile_expected
# --------------------------------------------------------------------------


def test_clean_ledger_matches_expectations(recon_db: str) -> None:
    charge, charge_expected = _charge("evt_1", "ch_1")
    refund, refund_expected = _refund("evt_2", "re_2")
    payout, payout_expected = _payout("evt_3", "po_3")
    for txn in (charge, refund, payout):
        post_transaction(recon_db, txn)

    expected = [
        charge_expected,
        refund_expected,
        payout_expected,
        _Expected("evt_4"),  # invoice.payment_failed: no money moved
    ]
    result = reconcile_expected(recon_db, expected)

    assert result.breaks == ()
    assert result.ok
    assert result.invariant_ok
    assert result.checked == 3  # money events only; the non-money one is correctly absent
    assert result.as_dict()["ok"] is True


def test_undelivered_event_is_missing_in_ledger(recon_db: str) -> None:
    """The DROP fault: the webhook never arrived, so no unit test can see it."""
    charge, charge_expected = _charge("evt_1", "ch_1")
    post_transaction(recon_db, charge)
    _, dropped = _payout("evt_2", "po_2", amount=5000)

    result = reconcile_expected(recon_db, [charge_expected, dropped])

    assert not result.ok
    assert result.invariant_ok  # conservation stays green: that is the whole point
    assert result.checked == 2
    assert len(result.breaks) == 1
    (found,) = result.breaks
    assert found.kind is BreakKind.MISSING_IN_LEDGER
    assert found.event_id == "evt_2"
    assert found.expected_minor == 5000
    assert found.actual_minor is None
    assert "evt_2" in found.detail


def test_unexpected_transaction_is_missing_in_source(recon_db: str) -> None:
    charge, charge_expected = _charge("evt_1", "ch_1")
    ghost, _ = _payout("evt_ghost", "po_ghost", amount=7000)
    for txn in (charge, ghost):
        post_transaction(recon_db, txn)

    result = reconcile_expected(recon_db, [charge_expected])

    assert not result.ok
    assert result.checked == 1
    assert len(result.breaks) == 1
    (found,) = result.breaks
    assert found.kind is BreakKind.MISSING_IN_SOURCE
    assert found.event_id == "evt_ghost"
    assert found.object_id == "po_ghost"
    assert found.actual_minor == 7000


def test_transaction_with_no_event_id_is_missing_in_source(recon_db: str) -> None:
    post_transaction(
        recon_db,
        LedgerTransaction(
            id=uuid.uuid4(),
            occurred_at=OCCURRED_AT,
            entries=(
                Entry("stripe_balance", "debit", 100),
                Entry("revenue", "credit", 100),
            ),
        ),
    )
    result = reconcile_expected(recon_db, [])

    assert _kinds(result) == [BreakKind.MISSING_IN_SOURCE]
    assert result.breaks[0].event_id is None
    assert result.checked == 0


def test_wrong_amount_is_amount_mismatch(recon_db: str) -> None:
    posted, _ = _charge("evt_1", "ch_1", gross=900, fee=59)
    _, expected = _charge("evt_1", "ch_1", gross=1000, fee=59)
    post_transaction(recon_db, posted)

    result = reconcile_expected(recon_db, [expected])

    assert not result.ok
    assert result.invariant_ok
    assert result.checked == 1
    assert set(_kinds(result)) == {BreakKind.AMOUNT_MISMATCH}
    by_account = {b.detail.split("account ")[1].split(" ")[0]: b for b in result.breaks}
    assert by_account["revenue"].expected_minor == 1000
    assert by_account["revenue"].actual_minor == 900
    assert by_account["stripe_balance"].expected_minor == 941
    assert by_account["stripe_balance"].actual_minor == 841
    assert all(b.event_id == "evt_1" and b.object_id == "ch_1" for b in result.breaks)
    assert "processing_fees" not in by_account  # the fee matched; only the gross moved


def test_non_money_expectations_never_break(recon_db: str) -> None:
    result = reconcile_expected(
        recon_db,
        [_Expected("evt_1"), _Expected("evt_2", {}), _Expected("evt_3", {"revenue": 0})],
    )

    assert result.ok
    assert result.breaks == ()
    assert result.checked == 0


def test_non_money_event_that_posted_money_is_a_break(recon_db: str) -> None:
    """The other half of "not every event is a money movement"."""
    charge, _ = _charge("evt_1", "ch_1")
    post_transaction(recon_db, charge)

    result = reconcile_expected(recon_db, [_Expected("evt_1")])

    assert not result.ok
    assert result.checked == 1
    assert set(_kinds(result)) == {BreakKind.AMOUNT_MISMATCH}
    assert all(b.expected_minor == 0 for b in result.breaks)


def test_duplicate_expectation_is_one_expectation(recon_db: str) -> None:
    """A duplicated delivery must post exactly once, so it is one expectation."""
    charge, expected = _charge("evt_1", "ch_1")
    post_transaction(recon_db, charge)

    result = reconcile_expected(recon_db, [expected, expected, expected])

    assert result.ok
    assert result.checked == 1


def test_wrong_account_keeps_the_invariant_green(recon_db: str) -> None:
    """Two balanced transactions, one posted to the wrong account.

    Money conservation cannot see this — every balanced transaction adds the
    same amount to both sides of the invariant whatever accounts it touches.
    Only the external comparison sees it, and the reconciler must report both
    facts honestly: invariant ok, ledger wrong.
    """
    good, good_expected = _charge("evt_1", "ch_1")
    _, expected_2 = _payout("evt_2", "po_2", amount=5000)
    wrong_account = _txn(
        "evt_2",
        "po_2",
        "payout.paid",
        (
            Entry("dispute_losses", "debit", 5000),  # should have been bank
            Entry("stripe_balance", "credit", 5000),
        ),
    )
    for txn in (good, wrong_account):
        post_transaction(recon_db, txn)

    result = reconcile_expected(recon_db, [good_expected, expected_2])

    assert result.invariant_ok is True
    assert not result.ok
    assert result.checked == 2
    kinds = {(b.kind, b.detail.split("account ")[1].split(" ")[0]) for b in result.breaks}
    assert kinds == {
        (BreakKind.AMOUNT_MISMATCH, "bank"),
        (BreakKind.AMOUNT_MISMATCH, "dispute_losses"),
    }


def test_unbalanced_ledger_reports_invariant_violation(recon_db: str) -> None:
    """Fabricate the one state the schema forbids, and check the report is honest.

    The deferred balance trigger makes a globally unbalanced ledger unreachable
    through post_transaction, so the test disables that trigger as superuser to
    manufacture the state the reconciler must be able to describe. Every other
    test reaches its state through the real write path.
    """
    charge, charge_expected = _charge("evt_1", "ch_1")
    post_transaction(recon_db, charge)

    txn_id = uuid.uuid4()
    with psycopg.connect(recon_db) as conn:
        conn.execute("ALTER TABLE entries DISABLE TRIGGER entries_balanced")
        conn.execute(
            "INSERT INTO transactions (id, stripe_event_id, stripe_object_id, event_type,"
            " occurred_at) VALUES (%s, %s, %s, %s, %s)",
            (txn_id, "evt_bad", "ch_bad", "charge.succeeded", OCCURRED_AT),
        )
        for account, amount in (("stripe_balance", 100), ("bank", 100)):
            conn.execute(
                "INSERT INTO entries (transaction_id, account_id, dir, amount_minor, currency)"
                " SELECT %s, id, 'debit', %s, 'USD' FROM accounts WHERE name = %s",
                (txn_id, amount, account),
            )
        conn.commit()
        conn.execute("ALTER TABLE entries ENABLE TRIGGER entries_balanced")

    result = reconcile_expected(recon_db, [charge_expected, _Expected("evt_bad", {})])

    assert result.invariant_ok is False
    assert not result.ok
    violations = [b for b in result.breaks if b.kind is BreakKind.INVARIANT_VIOLATION]
    assert len(violations) == 1
    assert violations[0].actual_minor - violations[0].expected_minor == 200
    assert "USD" in violations[0].detail


def test_reconcile_expected_never_writes(recon_db: str) -> None:
    """The reconciler holds a read-only transaction; a write through it fails."""
    charge, expected = _charge("evt_1", "ch_1")
    post_transaction(recon_db, charge)
    before = _snapshot(recon_db)

    reconcile_expected(recon_db, [expected])

    assert _snapshot(recon_db) == before
    with psycopg.connect(recon_db) as conn:
        conn.read_only = True  # the mechanism reconcile_* relies on
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(
                "INSERT INTO transactions (id, occurred_at) VALUES (%s, now())",
                (uuid.uuid4(),),
            )


def _snapshot(db_url: str) -> list[tuple]:
    with psycopg.connect(db_url) as conn:
        return conn.execute(
            "SELECT t.id, e.account_id, e.dir, e.amount_minor FROM transactions t"
            " LEFT JOIN entries e ON e.transaction_id = t.id ORDER BY t.id, e.id"
        ).fetchall()


# --------------------------------------------------------------------------
# reconcile_stripe
# --------------------------------------------------------------------------


def test_stripe_match_produces_no_breaks(recon_db: str) -> None:
    charge, _ = _charge("evt_1", "ch_1", gross=1000, fee=59)
    refund, _ = _refund("evt_2", "re_2", amount=400)
    payout, _ = _payout("evt_3", "po_3", amount=5000)
    dispute, _ = _dispute("evt_4", "dp_4", amount=2000, fee=1500)
    for txn in (charge, refund, payout, dispute):
        post_transaction(recon_db, txn)

    client = _StubStripe(
        _page(
            _bt("txn_1", "ch_1", 1000, 59, "charge"),
            _bt("txn_2", "re_2", -400, 0, "refund"),
            _bt("txn_3", "po_3", -5000, 0, "payout"),
            _bt("txn_4", "dp_4", -2000, 1500, "adjustment"),
        )
    )
    result = reconcile_stripe(recon_db, client)

    assert result.breaks == ()
    assert result.ok
    assert result.checked == 4
    assert client.calls[0][0] == "/v1/balance_transactions"
    assert client.calls[0][1]["limit"] == 100


def test_stripe_transaction_with_no_ledger_row(recon_db: str) -> None:
    charge, _ = _charge("evt_1", "ch_1")
    post_transaction(recon_db, charge)

    client = _StubStripe(
        _page(
            _bt("txn_1", "ch_1", 1000, 59),
            _bt("txn_2", "ch_never_delivered", 2500, 102),
        )
    )
    result = reconcile_stripe(recon_db, client)

    assert result.checked == 2
    assert _kinds(result) == [BreakKind.MISSING_IN_LEDGER]
    (found,) = result.breaks
    assert found.object_id == "ch_never_delivered"
    assert found.expected_minor == 2500
    assert found.actual_minor is None


def test_stripe_amount_difference(recon_db: str) -> None:
    charge, _ = _charge("evt_1", "ch_1", gross=1000, fee=59)
    post_transaction(recon_db, charge)

    result = reconcile_stripe(recon_db, _StubStripe(_page(_bt("txn_1", "ch_1", 1500, 59))))

    assert _kinds(result) == [BreakKind.AMOUNT_MISMATCH]
    (found,) = result.breaks
    assert found.expected_minor == 1500
    assert found.actual_minor == 1000
    assert found.event_id == "evt_1"
    assert found.object_id == "ch_1"


def test_stripe_fee_difference(recon_db: str) -> None:
    charge, _ = _charge("evt_1", "ch_1", gross=1000, fee=59)
    post_transaction(recon_db, charge)

    # amount still agrees (net + fee == 1000); only the fee split is wrong.
    result = reconcile_stripe(recon_db, _StubStripe(_page(_bt("txn_1", "ch_1", 1000, 99))))

    assert _kinds(result) == [BreakKind.FEE_MISMATCH]
    (found,) = result.breaks
    assert found.expected_minor == 99
    assert found.actual_minor == 59


def test_stripe_missing_in_source(recon_db: str) -> None:
    charge, _ = _charge("evt_1", "ch_1")
    post_transaction(recon_db, charge)

    result = reconcile_stripe(recon_db, _StubStripe(_page()))

    assert result.checked == 0
    assert _kinds(result) == [BreakKind.MISSING_IN_SOURCE]
    assert result.breaks[0].object_id == "ch_1"
    assert result.breaks[0].actual_minor == 1000


def test_stripe_pagination_follows_the_cursor(recon_db: str) -> None:
    charge, _ = _charge("evt_1", "ch_1")
    payout, _ = _payout("evt_2", "po_2", amount=5000)
    for txn in (charge, payout):
        post_transaction(recon_db, txn)

    client = _StubStripe(
        _page(_bt("txn_1", "ch_1", 1000, 59), has_more=True),
        _page(_bt("txn_2", "po_2", -5000, 0, "payout")),
    )
    result = reconcile_stripe(recon_db, client, limit=1, created_gte=OCCURRED_TS - 60)

    assert result.ok
    assert result.checked == 2
    assert len(client.calls) == 2
    assert "starting_after" not in client.calls[0][1]
    assert client.calls[0][1] == {"limit": 1, "created": {"gte": OCCURRED_TS - 60}}
    assert client.calls[1][1]["starting_after"] == "txn_1"


def test_stripe_pagination_stops_on_a_stuck_cursor(recon_db: str) -> None:
    """A source that always claims has_more must not page forever."""
    charge, _ = _charge("evt_1", "ch_1")
    post_transaction(recon_db, charge)
    page = _page(_bt("txn_1", "ch_1", 1000, 59), has_more=True)
    client = _StubStripe(page, page, page)

    result = reconcile_stripe(recon_db, client)

    assert len(client.calls) == 2  # second page repeats the cursor -> stop
    assert result.checked == 2  # the same bt twice: counted honestly, matched once
    assert result.breaks == ()


def test_stripe_window_excludes_older_ledger_rows(recon_db: str) -> None:
    entries = (
        Entry("stripe_balance", "debit", 941),
        Entry("processing_fees", "debit", 59),
        Entry("revenue", "credit", 1000),
    )
    post_transaction(
        recon_db,
        _txn(
            "evt_old",
            "ch_old",
            "charge.succeeded",
            entries,
            occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
        ),
    )
    post_transaction(
        recon_db,
        _txn(
            "evt_1",
            "ch_1",
            "charge.succeeded",
            entries,
            occurred_at=datetime(2026, 8, 4, 18, 0, tzinfo=UTC),
        ),
    )

    window_start = int(datetime(2026, 8, 4, 17, 0, tzinfo=UTC).timestamp())
    client = _StubStripe(_page(_bt("txn_1", "ch_1", 1000, 59)))
    result = reconcile_stripe(recon_db, client, created_gte=window_start)

    assert result.ok  # the 2020 transaction is outside the window, not a break
    assert result.checked == 1


def test_stripe_reports_invariant_alongside_the_diff(recon_db: str) -> None:
    charge, _ = _charge("evt_1", "ch_1")
    post_transaction(recon_db, charge)

    result = reconcile_stripe(recon_db, _StubStripe(_page(_bt("txn_1", "ch_1", 1000, 59))))

    assert result.invariant_ok is True
    assert result.as_dict()["invariant_ok"] is True

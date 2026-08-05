"""The DATABASE, not application code, enforces the ledger's rules.

Every statement here is raw SQL on a plain psycopg connection — no ledgerproof
code in the write path — so a passing test is unambiguous evidence that the
triggers are the enforcer.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest


def _insert_txn(conn: psycopg.Connection, txn_id: uuid.UUID) -> None:
    conn.execute("INSERT INTO transactions (id, occurred_at) VALUES (%s, now())", (txn_id,))


def _insert_entry(
    conn: psycopg.Connection, txn_id: uuid.UUID, account: str, dir_: str, amount: int
) -> None:
    conn.execute(
        "INSERT INTO entries (transaction_id, account_id, dir, amount_minor, currency)"
        " SELECT %s, id, %s, %s, 'USD' FROM accounts WHERE name = %s",
        (txn_id, dir_, amount, account),
    )


def _seed_valid_txn(conn: psycopg.Connection) -> uuid.UUID:
    txn_id = uuid.uuid4()
    _insert_txn(conn, txn_id)
    _insert_entry(conn, txn_id, "stripe_balance", "debit", 500)
    _insert_entry(conn, txn_id, "revenue", "credit", 500)
    conn.commit()
    return txn_id


def _assert_counts(db_url: str, transactions: int, entries: int) -> None:
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == transactions
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == entries


def test_unbalanced_transaction_rejected_at_commit(ledger_db: str) -> None:
    conn = psycopg.connect(ledger_db)
    try:
        txn_id = uuid.uuid4()
        _insert_txn(conn, txn_id)
        _insert_entry(conn, txn_id, "stripe_balance", "debit", 1000)
        _insert_entry(conn, txn_id, "revenue", "credit", 900)
        # The deferred constraint trigger judges the whole transaction at COMMIT.
        with pytest.raises(psycopg.errors.RaiseException) as excinfo:
            conn.commit()
        assert "unbalanced" in str(excinfo.value)
    finally:
        conn.close()
    _assert_counts(ledger_db, transactions=0, entries=0)


def test_update_on_entries_raises_append_only(ledger_conn: psycopg.Connection) -> None:
    _seed_valid_txn(ledger_conn)
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        ledger_conn.execute("UPDATE entries SET amount_minor = amount_minor + 1")
    assert "append-only" in str(excinfo.value)
    ledger_conn.rollback()
    assert ledger_conn.execute("SELECT sum(amount_minor) FROM entries").fetchone()[0] == 1000


def test_delete_on_entries_raises_append_only(ledger_conn: psycopg.Connection) -> None:
    _seed_valid_txn(ledger_conn)
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        ledger_conn.execute("DELETE FROM entries")
    assert "append-only" in str(excinfo.value)
    ledger_conn.rollback()
    assert ledger_conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 2


def test_update_on_transactions_raises_append_only(ledger_conn: psycopg.Connection) -> None:
    _seed_valid_txn(ledger_conn)
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        ledger_conn.execute("UPDATE transactions SET memo = 'rewritten history'")
    assert "append-only" in str(excinfo.value)
    ledger_conn.rollback()


def test_delete_on_transactions_raises_append_only(ledger_conn: psycopg.Connection) -> None:
    _seed_valid_txn(ledger_conn)
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        ledger_conn.execute("DELETE FROM transactions")
    assert "append-only" in str(excinfo.value)
    ledger_conn.rollback()
    assert ledger_conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1


def test_truncate_entries_raises_append_only(ledger_conn: psycopg.Connection) -> None:
    _seed_valid_txn(ledger_conn)
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        ledger_conn.execute("TRUNCATE entries")
    assert "append-only" in str(excinfo.value)
    ledger_conn.rollback()
    assert ledger_conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 2


def test_truncate_transactions_cascade_raises_append_only(
    ledger_conn: psycopg.Connection,
) -> None:
    _seed_valid_txn(ledger_conn)
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        ledger_conn.execute("TRUNCATE transactions CASCADE")
    assert "append-only" in str(excinfo.value)
    ledger_conn.rollback()


def test_cross_currency_entry_rejected_by_composite_fk(ledger_conn: psycopg.Connection) -> None:
    """An entry's currency must match its account's currency — enforced by the
    (account_id, currency) -> accounts (id, currency) composite FK, so the
    statement fails immediately, before any deferred trigger gets involved."""
    txn_id = uuid.uuid4()
    _insert_txn(ledger_conn, txn_id)
    with pytest.raises(psycopg.errors.ForeignKeyViolation) as excinfo:
        ledger_conn.execute(
            "INSERT INTO entries (transaction_id, account_id, dir, amount_minor, currency)"
            " SELECT %s, id, 'debit', 500, 'EUR' FROM accounts WHERE name = 'stripe_balance'",
            (txn_id,),
        )
    assert "entries_currency_matches_account" in str(excinfo.value)
    ledger_conn.rollback()
    assert ledger_conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


# UPDATE/DELETE as ledger_app may die on privilege (role lacks the grant) or on
# the append-only trigger (if a grant ever crept back in) — either is the wall.
_MUTATION_WALL = (psycopg.errors.InsufficientPrivilege, psycopg.errors.RaiseException)


def test_ledger_app_can_insert_but_not_mutate_ledger(app_db_url: str) -> None:
    """Least-privilege proof over the actual application role."""
    conn = psycopg.connect(app_db_url)
    try:
        txn_id = uuid.uuid4()
        _insert_txn(conn, txn_id)
        _insert_entry(conn, txn_id, "stripe_balance", "debit", 500)
        _insert_entry(conn, txn_id, "revenue", "credit", 500)
        conn.commit()  # a balanced pair posts fine under the app role
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 2

        with pytest.raises(_MUTATION_WALL):
            conn.execute("UPDATE entries SET amount_minor = amount_minor + 1")
        conn.rollback()
        with pytest.raises(_MUTATION_WALL):
            conn.execute("DELETE FROM entries")
        conn.rollback()
        with pytest.raises(_MUTATION_WALL):
            conn.execute("UPDATE transactions SET memo = 'rewritten history'")
        conn.rollback()
        with pytest.raises(_MUTATION_WALL):
            conn.execute("DELETE FROM transactions")
        conn.rollback()

        assert conn.execute("SELECT sum(amount_minor) FROM entries").fetchone()[0] == 1000
    finally:
        conn.close()


def test_ledger_app_stripe_events_update_limited_to_status(app_db_url: str) -> None:
    """The worker's only legitimate stripe_events mutation is status."""
    conn = psycopg.connect(app_db_url)
    try:
        conn.execute(
            "INSERT INTO stripe_events (id, event_type, object_id, payload)"
            " VALUES ('evt_priv_1', 'charge.succeeded', 'ch_priv_1', '{}'::jsonb)"
        )
        conn.commit()

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE stripe_events SET payload = '{\"forged\": true}'::jsonb"
                " WHERE id = 'evt_priv_1'"
            )
        conn.rollback()

        conn.execute("UPDATE stripe_events SET status = 'processed' WHERE id = 'evt_priv_1'")
        conn.commit()
        row = conn.execute(
            "SELECT status, payload FROM stripe_events WHERE id = 'evt_priv_1'"
        ).fetchone()
        assert row == ("processed", {})
    finally:
        conn.close()


def test_zero_entry_transaction_rejected_at_commit(ledger_db: str) -> None:
    conn = psycopg.connect(ledger_db)
    try:
        _insert_txn(conn, uuid.uuid4())
        with pytest.raises(psycopg.errors.RaiseException) as excinfo:
            conn.commit()
        assert "at least 2" in str(excinfo.value)
    finally:
        conn.close()
    _assert_counts(ledger_db, transactions=0, entries=0)


def test_single_entry_transaction_rejected_at_commit(ledger_db: str) -> None:
    conn = psycopg.connect(ledger_db)
    try:
        txn_id = uuid.uuid4()
        _insert_txn(conn, txn_id)
        _insert_entry(conn, txn_id, "stripe_balance", "debit", 100)
        with pytest.raises(psycopg.errors.RaiseException) as excinfo:
            conn.commit()
        # Deferred triggers fire in event order, so min-entries reports first;
        # accept the balance trigger's message too — either way the DB refused.
        msg = str(excinfo.value)
        assert "at least 2" in msg or "unbalanced" in msg
    finally:
        conn.close()
    _assert_counts(ledger_db, transactions=0, entries=0)

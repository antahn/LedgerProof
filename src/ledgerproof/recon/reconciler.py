"""Reconcile the ledger against Stripe's own records.

Contract: pull balance_transactions from the Stripe API (test mode), diff each
against the ledger's transactions/entries, and emit breaks (missing here,
missing there, amount mismatch) plus a global invariant check. This is the
mechanism that catches what tests miss — e.g. the DROP fault, where a webhook
never arrives and only an external comparison can notice. Read-only against
the ledger; never repairs, only reports.

Two sources of truth, one diff engine:

- reconcile_expected(db_url, expected) — the source is what the caller KNOWS it
  delivered (the chaos harness's own fixtures). Hermetic, no network. This is
  what makes DROP measurable: an expected event with no transaction is a
  MISSING_IN_LEDGER break, and no unit test can see that by construction.
- reconcile_stripe(db_url, client) — the source is Stripe's
  /v1/balance_transactions. This is the production shape.

Why the global invariant cannot substitute for either: every balanced
transaction contributes +amount to the debit-normal sum and +amount to the
credit-normal sum (an entry adds +amount to its side when dir='debit' and
-amount when dir='credit', whatever its account's normal), so ANY set of
per-transaction-balanced rows keeps the invariant green — including a payment
posted twice, posted to the wrong account, or never posted at all. The
invariant proves internal consistency; only an external comparison proves
agreement with reality. Both are reported here, separately and honestly.

The Stripe comparison uses one identity: a balance transaction IS a change to
the Stripe balance. bt.net must equal the ledger's signed delta on
stripe_balance for that object, and bt.amount == bt.net + bt.fee — which holds
for charges (fee inside the gross), refunds, disputes and payouts alike, so no
per-event-type special-casing is needed.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from ledgerproof.ledger.invariant import InvariantResult
from ledgerproof.ledger.invariant import check as invariant_check
from ledgerproof.recon.breaks import Break, BreakKind, ReconResult

__all__ = [
    "Break",
    "BreakKind",
    "ExpectedEvent",
    "ReconResult",
    "reconcile_expected",
    "reconcile_stripe",
]

_FEE_ACCOUNT = "processing_fees"
_STRIPE_BALANCE_ACCOUNT = "stripe_balance"


class ExpectedEvent(Protocol):
    """An event the caller knows it delivered, with the delta it should cause.

    harness.events.EventFixture satisfies this structurally. Duck-typed on
    purpose: src/ never imports harness/ — the system under test must not
    depend on its adversary.
    """

    @property
    def event_id(self) -> str: ...

    @property
    def expected_delta(self) -> Mapping[str, int]: ...


@dataclass(frozen=True)
class _LedgerTxn:
    """One ledger transaction reduced to its per-account signed net effect."""

    txn_id: str
    event_id: str | None
    object_id: str | None
    event_type: str | None
    delta: dict[str, int]  # account name -> signed minor units, view sign convention
    debit_total: int  # the transaction's size (debits == credits)


@contextmanager
def _readonly(db_url_or_conn: str | psycopg.Connection) -> Iterator[psycopg.Connection]:
    """Yield a connection the reconciler cannot write through.

    When we own the connection we mark it read-only, so "the reconciler never
    repairs" is enforced by the database rather than by reviewer attention. A
    caller-supplied connection is used as-is and left as it was found.
    """
    if isinstance(db_url_or_conn, str):
        with psycopg.connect(db_url_or_conn) as conn:
            conn.read_only = True
            yield conn
    else:
        yield db_url_or_conn


def _load_ledger(
    conn: psycopg.Connection,
    *,
    currency: str | None = None,
    since: int | None = None,
) -> list[_LedgerTxn]:
    """Every ledger transaction as a per-account signed delta.

    The sign expression is character-for-character the one in the
    account_balances view (entry direction against the account's normal), so
    the reconciler and the balances can never disagree about which way money
    moved. ``since`` is a unix timestamp filtering on occurred_at.
    """
    where: list[str] = []
    params: list[Any] = []
    if currency is not None:
        where.append("a.currency = %s")
        params.append(currency)
    if since is not None:
        where.append("t.occurred_at >= to_timestamp(%s)")
        params.append(since)

    sql = (
        "SELECT t.id::text, t.stripe_event_id, t.stripe_object_id, t.event_type, a.name,"
        "       SUM(CASE WHEN e.dir = a.normal"
        "                THEN e.amount_minor ELSE -e.amount_minor END) AS net_minor,"
        "       SUM(CASE WHEN e.dir = 'debit' THEN e.amount_minor ELSE 0 END) AS debit_minor"
        "  FROM transactions t"
        "  JOIN entries e ON e.transaction_id = t.id"
        "  JOIN accounts a ON a.id = e.account_id"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " GROUP BY t.id, t.stripe_event_id, t.stripe_object_id, t.event_type, a.name,"
        "          t.occurred_at"
        " ORDER BY t.occurred_at, t.id, a.name"
    )

    grouped: dict[str, dict[str, Any]] = {}
    for txn_id, event_id, object_id, event_type, account, net, debit in conn.execute(sql, params):
        txn = grouped.setdefault(
            txn_id,
            {
                "event_id": event_id,
                "object_id": object_id,
                "event_type": event_type,
                "delta": {},
                "debit_total": 0,
            },
        )
        if int(net) != 0:  # an account a transaction moved and moved back is not a delta
            txn["delta"][account] = int(net)
        txn["debit_total"] += int(debit)
    return [_LedgerTxn(txn_id=txn_id, **fields) for txn_id, fields in grouped.items()]


def _magnitude(delta: Mapping[str, int]) -> int:
    """The size of a transaction implied by a signed per-account delta.

    Double entry puts every minor unit on exactly two sides, so the L1 norm of
    the delta is twice the transaction's debit total.
    """
    return sum(abs(v) for v in delta.values()) // 2


def _fmt(delta: Mapping[str, int]) -> str:
    return "{" + ", ".join(f"{k}={v:+d}" for k, v in sorted(delta.items())) + "}"


def _invariant_breaks(inv: InvariantResult) -> list[Break]:
    return [
        Break(
            kind=BreakKind.INVARIANT_VIOLATION,
            expected_minor=c.sum_credit_normal,
            actual_minor=c.sum_debit_normal,
            detail=(
                f"{c.currency}: debit-normal {c.sum_debit_normal} != "
                f"credit-normal {c.sum_credit_normal} (difference {c.difference})"
            ),
        )
        for c in inv.per_currency
        if not c.ok
    ]


def reconcile_expected(
    db_url: str | psycopg.Connection,
    expected: Sequence[ExpectedEvent],
    *,
    currency: str = "USD",
) -> ReconResult:
    """Diff the ledger against the events the caller knows it delivered.

    Each expected event is matched by stripe_event_id:
    - money event (non-zero delta) with no transaction -> MISSING_IN_LEDGER.
      This is the DROP signal, invisible to every unit test by construction.
    - non-money event (empty or all-zero delta) with no transaction -> correct,
      not a break. Recording a transaction for a non-money event is itself a
      way to create money from nothing, so the absence is the expectation.
    - matched event whose per-account net effect differs -> one AMOUNT_MISMATCH
      per differing account, carrying both numbers.
    - a ledger transaction whose stripe_event_id was never delivered (or which
      carries no event id at all) -> MISSING_IN_SOURCE.

    Repeated event ids in ``expected`` are collapsed: a duplicate delivery must
    post exactly once, so it is one expectation.

    ``checked`` counts events actually compared — money events, plus non-money
    events that unexpectedly turned up in the ledger.

    Harness callers pass the fixtures their scenario's Expectation says should
    have posted; a delivery the scenario expected ingest to REJECT (tampered,
    stale, downgraded) must not appear here, or its correct absence is reported
    as a break.
    """
    with _readonly(db_url) as conn:
        # One read-only transaction: the diff and the invariant see the same snapshot.
        ledger = _load_ledger(conn, currency=currency)
        inv = invariant_check(conn)

    by_event = {t.event_id: t for t in ledger if t.event_id is not None}
    breaks: list[Break] = []
    seen: set[str] = set()
    checked = 0

    for item in expected:
        event_id = item.event_id
        if event_id in seen:
            continue
        seen.add(event_id)
        delta = {a: int(v) for a, v in dict(item.expected_delta).items() if int(v) != 0}
        txn = by_event.get(event_id)

        if txn is None:
            if not delta:
                continue
            checked += 1
            breaks.append(
                Break(
                    kind=BreakKind.MISSING_IN_LEDGER,
                    event_id=event_id,
                    expected_minor=_magnitude(delta),
                    detail=(
                        f"expected event {event_id} produced no ledger transaction; "
                        f"expected delta {_fmt(delta)}"
                    ),
                )
            )
            continue

        checked += 1
        for account in sorted(set(delta) | set(txn.delta)):
            want = delta.get(account, 0)
            got = txn.delta.get(account, 0)
            if want != got:
                breaks.append(
                    Break(
                        kind=BreakKind.AMOUNT_MISMATCH,
                        object_id=txn.object_id,
                        event_id=event_id,
                        expected_minor=want,
                        actual_minor=got,
                        detail=(
                            f"event {event_id}: account {account} moved {got:+d} minor units, "
                            f"expected {want:+d}"
                        ),
                    )
                )

    for txn in ledger:
        if txn.event_id is not None and txn.event_id in seen:
            continue
        breaks.append(
            Break(
                kind=BreakKind.MISSING_IN_SOURCE,
                object_id=txn.object_id,
                event_id=txn.event_id,
                actual_minor=txn.debit_total,
                detail=(
                    f"ledger transaction {txn.txn_id} "
                    f"(event {txn.event_id}, object {txn.object_id}, type {txn.event_type}) "
                    f"has no matching delivered event; ledger delta {_fmt(txn.delta)}"
                ),
            )
        )

    breaks.extend(_invariant_breaks(inv))
    return ReconResult(breaks=tuple(breaks), checked=checked, invariant_ok=inv.ok)


def _iter_balance_transactions(
    client: Any, *, limit: int, created_gte: int | None
) -> Iterator[dict]:
    """Page through /v1/balance_transactions with Stripe's cursor pagination."""
    base: dict[str, Any] = {"limit": limit}
    if created_gte is not None:
        base["created"] = {"gte": created_gte}

    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        params = dict(base)
        if cursor is not None:
            params["starting_after"] = cursor
        page = client.get("/v1/balance_transactions", params)
        data = page.get("data") or []
        yield from data
        if not page.get("has_more") or not data:
            return
        cursor = data[-1].get("id")
        # A cursor that does not advance would page forever; stop instead.
        if not isinstance(cursor, str) or cursor in seen_cursors:
            return
        seen_cursors.add(cursor)


def _source_id(bt: Mapping[str, Any]) -> str | None:
    source = bt.get("source")
    if isinstance(source, dict):  # expand[]=source returns the object, not the id
        source = source.get("id")
    return source if isinstance(source, str) else None


def reconcile_stripe(
    db_url: str | psycopg.Connection,
    client: Any,
    *,
    limit: int = 100,
    created_gte: int | None = None,
) -> ReconResult:
    """Diff the ledger against Stripe's own /v1/balance_transactions.

    ``client`` is anything with ``.get(path, params) -> dict``
    (stripe_io.client.StripeEgressClient satisfies it). Each balance
    transaction is matched to the ledger by ``bt["source"]`` against
    transactions.stripe_object_id — the same money-movement identity the
    worker posts with (a refund's re_..., a dispute's dp_..., not the parent
    charge) — and compared on two numbers:

        ledger fee    = signed delta on processing_fees   vs bt["fee"]
        ledger amount = signed delta on stripe_balance + ledger fee
                                                        vs bt["amount"]

    because a balance transaction is by definition a change to the Stripe
    balance, with net = amount - fee.

    Unmatched in either direction becomes MISSING_IN_LEDGER / MISSING_IN_SOURCE.
    ``created_gte`` bounds BOTH sides of the diff — Stripe's created filter and
    the ledger's occurred_at — so a transaction older than the window is not
    reported as missing from a page that was never asked to contain it.
    Ledger transactions with no stripe_object_id are OUT OF SCOPE for this diff
    and are not reported: they are compensating entries with no Stripe
    counterpart by construction. reconcile_expected and the invariant still see
    them; this exclusion is stated rather than silent.
    """
    with _readonly(db_url) as conn:
        ledger = _load_ledger(conn, since=created_gte)
        inv = invariant_check(conn)

    by_object: dict[str, list[_LedgerTxn]] = {}
    for txn in ledger:
        if txn.object_id is not None:
            by_object.setdefault(txn.object_id, []).append(txn)

    breaks: list[Break] = []
    matched: set[str] = set()
    checked = 0

    for bt in _iter_balance_transactions(client, limit=limit, created_gte=created_gte):
        checked += 1
        bt_id = bt.get("id")
        source_id = _source_id(bt)
        amount = int(bt.get("amount", 0))
        fee = int(bt.get("fee", 0))

        candidates = by_object.get(source_id, []) if source_id is not None else []
        txn = next((c for c in candidates if c.txn_id not in matched), None)
        if txn is None and candidates:
            txn = candidates[0]  # a second balance transaction on the same object

        if txn is None:
            breaks.append(
                Break(
                    kind=BreakKind.MISSING_IN_LEDGER,
                    object_id=source_id if source_id is not None else bt_id,
                    expected_minor=amount,
                    detail=(
                        f"balance transaction {bt_id} (type {bt.get('type')}, "
                        f"source {source_id}) has no ledger transaction"
                    ),
                )
            )
            continue

        matched.add(txn.txn_id)
        ledger_fee = txn.delta.get(_FEE_ACCOUNT, 0)
        ledger_amount = txn.delta.get(_STRIPE_BALANCE_ACCOUNT, 0) + ledger_fee
        if ledger_amount != amount:
            breaks.append(
                Break(
                    kind=BreakKind.AMOUNT_MISMATCH,
                    object_id=source_id,
                    event_id=txn.event_id,
                    expected_minor=amount,
                    actual_minor=ledger_amount,
                    detail=(
                        f"balance transaction {bt_id}: Stripe amount {amount:+d}, "
                        f"ledger {ledger_amount:+d} (delta {_fmt(txn.delta)})"
                    ),
                )
            )
        if ledger_fee != fee:
            breaks.append(
                Break(
                    kind=BreakKind.FEE_MISMATCH,
                    object_id=source_id,
                    event_id=txn.event_id,
                    expected_minor=fee,
                    actual_minor=ledger_fee,
                    detail=f"balance transaction {bt_id}: Stripe fee {fee}, ledger {ledger_fee}",
                )
            )

    for txn in ledger:
        if txn.object_id is None or txn.txn_id in matched:
            continue
        breaks.append(
            Break(
                kind=BreakKind.MISSING_IN_SOURCE,
                object_id=txn.object_id,
                event_id=txn.event_id,
                actual_minor=txn.debit_total,
                detail=(
                    f"ledger transaction {txn.txn_id} (object {txn.object_id}, "
                    f"type {txn.event_type}) has no Stripe balance transaction in this window"
                ),
            )
        )

    breaks.extend(_invariant_breaks(inv))
    return ReconResult(breaks=tuple(breaks), checked=checked, invariant_ok=inv.ok)

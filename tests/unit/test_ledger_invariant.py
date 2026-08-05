"""The money-conservation invariant, property-tested over >=1,000 randomized
balanced transactions posted through the real write path (brief §6 Phase 1)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ledgerproof.ledger.invariant import CurrencyCheck, InvariantResult, check
from ledgerproof.ledger.post import Entry, LedgerTransaction, PostOutcome, post_transaction

SEEDED_ACCOUNTS = (
    "stripe_balance",
    "bank",
    "processing_fees",
    "dispute_losses",
    "refunds_contra",
    "revenue",
    "customer_liability",
)

# The gate requires >=1,000 posted transactions in the run. A single Hypothesis
# example carrying all 1,000 specs exceeds the generator's entropy budget and
# stalls, so the total is accumulated across examples instead:
# EXAMPLES x SPECS_PER_EXAMPLE = 1,000 exactly, counted in _posted_total and
# asserted by test_at_least_1000_transactions_were_posted below.
SPECS_PER_EXAMPLE = 50
EXAMPLES = 20
REQUIRED_TOTAL = 1000
assert EXAMPLES * SPECS_PER_EXAMPLE >= REQUIRED_TOTAL

_posted_total = 0

Side = list[tuple[str, int]]  # [(account_name, amount_minor), ...]


@st.composite
def _side(draw: st.DrawFn, total: int) -> Side:
    """Split `total` into 1-3 positive parts, each assigned a random account."""
    k = min(draw(st.integers(min_value=1, max_value=3)), total)
    if k == 1:
        parts = [total]
    else:
        cuts = sorted(
            draw(st.sets(st.integers(min_value=1, max_value=total - 1), min_size=k - 1, max_size=k - 1))
        )
        bounds = [0, *cuts, total]
        parts = [hi - lo for lo, hi in zip(bounds, bounds[1:])]
    return [(draw(st.sampled_from(SEEDED_ACCOUNTS)), part) for part in parts]


@st.composite
def _txn_spec(draw: st.DrawFn) -> tuple[Side, Side]:
    """A balanced transaction: debit and credit sides both summing to `total`."""
    total = draw(st.integers(min_value=1, max_value=10_000_000))
    return draw(_side(total)), draw(_side(total))


@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        # The ledger is append-only, so the DB deliberately accumulates across
        # examples — that is the sequence under test, not fixture leakage.
        HealthCheck.function_scoped_fixture,
    ],
)
@given(specs=st.lists(_txn_spec(), min_size=SPECS_PER_EXAMPLE, max_size=SPECS_PER_EXAMPLE))
def test_invariant_holds_after_randomized_transactions(
    ledger_db: str, specs: list[tuple[Side, Side]]
) -> None:
    global _posted_total
    for debits, credits in specs:
        entries = tuple(Entry(acct, "debit", amt) for acct, amt in debits) + tuple(
            Entry(acct, "credit", amt) for acct, amt in credits
        )
        txn = LedgerTransaction(
            id=uuid.uuid4(), occurred_at=datetime.now(timezone.utc), entries=entries
        )
        assert post_transaction(ledger_db, txn) is PostOutcome.POSTED
        _posted_total += 1

    result = check(ledger_db)
    assert result.ok, f"money was created or destroyed: {result.as_dict()}"
    # All seeded accounts are USD; the two sums must match exactly.
    (usd,) = result.per_currency
    assert usd.currency == "USD"
    assert usd.sum_debit_normal == usd.sum_credit_normal
    assert usd.difference == 0


def test_at_least_1000_transactions_were_posted() -> None:
    """Explicit gate: the property run above must actually have posted >=1,000."""
    assert _posted_total >= REQUIRED_TOTAL, (
        f"property test posted only {_posted_total} transactions; "
        f"the G1 gate requires >={REQUIRED_TOTAL}"
    )


def test_invariant_ok_on_empty_ledger(ledger_db: str) -> None:
    result = check(ledger_db)
    assert result.ok
    (usd,) = result.per_currency
    assert (usd.currency, usd.sum_debit_normal, usd.sum_credit_normal) == ("USD", 0, 0)


def test_currency_check_difference_and_ok() -> None:
    good = CurrencyCheck(currency="USD", sum_debit_normal=100, sum_credit_normal=100)
    assert good.difference == 0
    assert good.ok

    bad = CurrencyCheck(currency="USD", sum_debit_normal=970, sum_credit_normal=1000)
    assert bad.difference == -30
    assert not bad.ok
    assert not InvariantResult(per_currency=(good, bad)).ok


def test_invariant_result_as_dict_is_json_serializable() -> None:
    result = InvariantResult(
        per_currency=(CurrencyCheck(currency="USD", sum_debit_normal=5, sum_credit_normal=5),)
    )
    payload = json.loads(json.dumps(result.as_dict()))
    assert payload == {
        "ok": True,
        "per_currency": [
            {
                "currency": "USD",
                "sum_debit_normal": 5,
                "sum_credit_normal": 5,
                "difference": 0,
                "ok": True,
            }
        ],
    }

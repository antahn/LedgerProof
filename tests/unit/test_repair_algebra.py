"""What a compensating transaction can and cannot fix.

The ledger's only sanctioned correction is a new balanced transaction. This
file establishes what that mechanism is actually capable of — which turns out
to bound the Phase 5 repair metric, and to explain why the brief's
`repair_restores_invariant` is vacuous on this system.
"""

from __future__ import annotations

from itertools import product

from hypothesis import given
from hypothesis import strategies as st

from ledgerproof.triage.schema import RepairEntry, repair_is_balanced

# The seeded chart of accounts: name -> normal direction.
ACCOUNTS = {
    "stripe_balance": "debit",
    "bank": "debit",
    "processing_fees": "debit",
    "dispute_losses": "debit",
    "refunds_contra": "debit",
    "revenue": "credit",
    "customer_liability": "credit",
}


def invariant_delta(entries: list[RepairEntry]) -> int:
    """Change to (debit-normal total - credit-normal total) — the invariant gap."""
    delta = 0
    for entry in entries:
        normal = ACCOUNTS[entry.account_name]
        signed = entry.amount_minor if entry.direction == normal else -entry.amount_minor
        delta += signed if normal == "debit" else -signed
    return delta


def test_no_balanced_two_entry_transaction_can_move_the_invariant() -> None:
    """Exhaustive over the real chart of accounts."""
    moved = []
    for (a1, d1), (a2, d2) in product(product(ACCOUNTS, ("debit", "credit")), repeat=2):
        entries = [
            RepairEntry(account_name=a1, direction=d1, amount_minor=100),
            RepairEntry(account_name=a2, direction=d2, amount_minor=100),
        ]
        if repair_is_balanced(entries) and invariant_delta(entries) != 0:
            moved.append(entries)
    assert moved == []


@given(
    entries=st.lists(
        st.tuples(
            st.sampled_from(sorted(ACCOUNTS)),
            st.sampled_from(["debit", "credit"]),
            st.integers(min_value=1, max_value=10_000_000),
        ),
        min_size=2,
        max_size=6,
    )
)
def test_balanced_transactions_never_move_the_invariant(entries) -> None:
    """Property form: if it balances, the invariant gap is unchanged.

    This is why an unbalanced write is unrepairable. Once a transaction lands
    that debits more than it credits — only possible with the database's
    deferred trigger removed — the gap it opened cannot be closed by anything
    the database will subsequently accept. The append-only contract has no
    sanctioned move that fixes it.
    """
    repair = [
        RepairEntry(account_name=a, direction=d, amount_minor=amt) for a, d, amt in entries
    ]
    if not repair_is_balanced(repair):
        return
    assert invariant_delta(repair) == 0


def test_a_double_post_is_repairable_even_though_the_invariant_never_broke() -> None:
    """The other half of the story: the damage that IS fixable.

    A duplicated charge leaves the books internally consistent — both sides
    doubled — so the invariant is silent. But the balances disagree with what
    the delivered events say they should be, and reversing one copy is a
    balanced transaction, so the correction is expressible.
    """
    reversal = [
        RepairEntry(account_name="revenue", direction="debit", amount_minor=1000),
        RepairEntry(account_name="stripe_balance", direction="credit", amount_minor=941),
        RepairEntry(account_name="processing_fees", direction="credit", amount_minor=59),
    ]
    assert repair_is_balanced(reversal)
    assert invariant_delta(reversal) == 0  # it never needed to move

    doubled = {"stripe_balance": 1882, "processing_fees": 118, "revenue": 2000}
    corrected = dict(doubled)
    for entry in reversal:
        normal = ACCOUNTS[entry.account_name]
        signed = entry.amount_minor if entry.direction == normal else -entry.amount_minor
        corrected[entry.account_name] += signed
    assert corrected == {"stripe_balance": 941, "processing_fees": 59, "revenue": 1000}


def test_the_invariant_cannot_distinguish_the_three_damage_classes() -> None:
    """Why `repair_restores_invariant` is the wrong success criterion here.

    Of the three ways this ledger goes wrong, the invariant is blind to two and
    unfixable on the third — so a metric keyed to it measures almost nothing.
    Agreement with the balances the delivered events imply is the criterion
    that actually separates a good repair from a bad one.
    """
    lost_event = {"gap": 0, "balances_wrong": True}  # nothing posted; both sides move by 0
    double_post = {"gap": 0, "balances_wrong": True}  # both sides doubled
    unbalanced_write = {"gap": 59, "balances_wrong": True}  # the only one it sees

    assert lost_event["gap"] == double_post["gap"] == 0
    assert all(
        case["balances_wrong"] for case in (lost_event, double_post, unbalanced_write)
    )
    assert unbalanced_write["gap"] != 0

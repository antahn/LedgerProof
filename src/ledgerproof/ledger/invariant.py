"""The global money-conservation invariant.

Contract: for each currency, Σ balances of debit-normal accounts must equal
Σ balances of credit-normal accounts. A mismatch means the system created or
destroyed money out of nothing. `check(conn) -> InvariantResult` returns, per
currency, both sums, the difference, and a boolean; it is asserted in every
test and by the reconciler, and its raw output is what harness runs record in
artifacts/. Read-only.
"""

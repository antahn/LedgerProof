"""Reconcile the ledger against Stripe's own records.

Contract: pull balance_transactions from the Stripe API (test mode), diff each
against the ledger's transactions/entries, and emit breaks (missing here,
missing there, amount mismatch) plus a global invariant check. This is the
mechanism that catches what tests miss — e.g. the DROP fault, where a webhook
never arrives and only an external comparison can notice. Read-only against
the ledger; never repairs, only reports.
"""

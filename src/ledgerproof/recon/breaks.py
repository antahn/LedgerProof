"""Break taxonomy and records.

Contract: typed representation of every reconciliation discrepancy —
missing_in_ledger, missing_in_stripe, amount_mismatch, fee_mismatch — with the
Stripe object ids and ledger transaction ids needed to reproduce. Serializes
to the JSONL shape harness runs record in artifacts/.
"""

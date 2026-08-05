"""Map Stripe events to balanced ledger transactions.

Contract (each row balances, per §4.3 of the brief):
- charge.succeeded / payment_intent.succeeded:
    DR stripe_balance (net), DR processing_fees (fee) / CR revenue (gross)
- charge.refunded:  DR refunds_contra / CR stripe_balance
- charge.dispute.created:
    DR dispute_losses (amount), DR processing_fees (dispute fee)
    / CR stripe_balance (amount + fee)
- payout.paid:      DR bank / CR stripe_balance
- invoice.payment_failed: NO money moved — an event-log row, NOT a transaction.
  Recording a transaction for a non-money event is itself a way to create
  money from nothing.

Amounts are Stripe minor units (int). Output is data (transaction + entries);
this module performs no I/O and never decides idempotency — that lives in
dedupe and in the database's unique constraints.
"""

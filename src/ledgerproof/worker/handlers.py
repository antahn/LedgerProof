"""Per-event-type handlers — order-independent by contract.

Stripe guarantees neither ordering nor exactly-once delivery. Handlers
therefore never assume a prior event has been seen: when a later event arrives
first and references an object we lack, the handler FETCHES the missing object
from the Stripe API using the IDs on hand rather than inventing state or
failing. Duplicate deliveries are no-ops. No handler mutates ledger rows;
corrections are new compensating transactions.
"""

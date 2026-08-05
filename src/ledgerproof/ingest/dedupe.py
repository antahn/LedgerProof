"""Webhook dedupe — both documented keys.

Contract: an event is a duplicate if EITHER key has been seen:
  1. event.id
  2. (event.type, data.object.id)
Stripe documents that two distinct Event objects can be generated for the same
underlying state change, so event.id alone is insufficient. This layer is
best-effort fast-path; the database's unique constraints on transactions are
the authoritative last line of defense (the CONCURRENT_DUPLICATE fault attacks
exactly the gap between the two).
"""

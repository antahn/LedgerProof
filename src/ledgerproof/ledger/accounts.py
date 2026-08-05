"""Chart-of-accounts access.

Contract: read-only lookup of accounts by name/id; exposes account kind and
normal direction so posting code can compute entry signs. Accounts are seeded
by migration; this module never creates or mutates them at runtime.
"""

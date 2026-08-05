"""Settings loaded from the environment / .env.

Contract: single source of configuration for every process (ingest, worker,
recon, harness). Secrets come only from the environment; a `STRIPE_SECRET_KEY`
that does not start with `sk_test_` is a hard startup error — this project is
test mode only, by rule.
"""

-- 004_hardening.sql — DB-level hardening for confirmed review findings.
--
-- (1) Cross-currency entries. Nothing enforced entry.currency == its
--     account's currency: the per-currency balance trigger groups by ENTRY
--     currency while the balance view groups by ACCOUNT currency, so an EUR
--     entry against a USD account could make drift invisible. Enforced
--     declaratively with a composite foreign key — every (account_id,
--     currency) pair on an entry must be an existing (id, currency) pair on
--     accounts. No trigger needed; the FK is the strongest available layer.
--
-- (2) stripe_events payload rewriting. ledger_app held table-wide UPDATE on
--     stripe_events, but the worker's only legitimate mutation is advancing
--     the status column. Narrow the grant to UPDATE (status) so payload
--     history is physically un-rewritable by the app role.

BEGIN;

ALTER TABLE accounts
  ADD CONSTRAINT accounts_id_currency_key UNIQUE (id, currency);

ALTER TABLE entries
  ADD CONSTRAINT entries_currency_matches_account
  FOREIGN KEY (account_id, currency) REFERENCES accounts (id, currency);

REVOKE UPDATE ON stripe_events FROM ledger_app;
GRANT UPDATE (status) ON stripe_events TO ledger_app;

COMMIT;

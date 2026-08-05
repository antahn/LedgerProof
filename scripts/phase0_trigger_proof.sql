-- Phase 0 proof (§10.5): the database, not application code, enforces the ledger's rules.
-- Run: Get-Content scripts/phase0_trigger_proof.sql | docker exec -i ledgerproof-db psql -U postgres -d ledgerproof
-- Raw output is recorded in artifacts/, never edited.

\echo === 1. UNBALANCED transaction (debit 1000, credit 900) — must be REJECTED at COMMIT ===
BEGIN;
INSERT INTO transactions (id, stripe_event_id, stripe_object_id, event_type, occurred_at, memo)
VALUES ('00000000-0000-4000-8000-000000000001', 'evt_proof_unbalanced', 'ch_proof_unbalanced',
        'charge.succeeded', now(), 'phase0 trigger proof: unbalanced');
INSERT INTO entries (transaction_id, account_id, dir, amount_minor, currency)
VALUES ('00000000-0000-4000-8000-000000000001',
        (SELECT id FROM accounts WHERE name='stripe_balance'), 'debit', 1000, 'USD');
INSERT INTO entries (transaction_id, account_id, dir, amount_minor, currency)
VALUES ('00000000-0000-4000-8000-000000000001',
        (SELECT id FROM accounts WHERE name='revenue'), 'credit', 900, 'USD');
COMMIT;

\echo === 2. BALANCED transaction (debit 970 net + 30 fee, credit 1000 gross) — must COMMIT ===
BEGIN;
INSERT INTO transactions (id, stripe_event_id, stripe_object_id, event_type, occurred_at, memo)
VALUES ('00000000-0000-4000-8000-000000000002', 'evt_proof_balanced', 'ch_proof_balanced',
        'charge.succeeded', now(), 'phase0 trigger proof: balanced');
INSERT INTO entries (transaction_id, account_id, dir, amount_minor, currency)
VALUES ('00000000-0000-4000-8000-000000000002',
        (SELECT id FROM accounts WHERE name='stripe_balance'), 'debit', 970, 'USD');
INSERT INTO entries (transaction_id, account_id, dir, amount_minor, currency)
VALUES ('00000000-0000-4000-8000-000000000002',
        (SELECT id FROM accounts WHERE name='processing_fees'), 'debit', 30, 'USD');
INSERT INTO entries (transaction_id, account_id, dir, amount_minor, currency)
VALUES ('00000000-0000-4000-8000-000000000002',
        (SELECT id FROM accounts WHERE name='revenue'), 'credit', 1000, 'USD');
COMMIT;

\echo === 3. UPDATE on entries — must RAISE ===
UPDATE entries SET amount_minor = 1
 WHERE transaction_id = '00000000-0000-4000-8000-000000000002';

\echo === 4. DELETE on entries — must RAISE ===
DELETE FROM entries
 WHERE transaction_id = '00000000-0000-4000-8000-000000000002';

\echo === 5. TRUNCATE entries — must RAISE ===
TRUNCATE entries;

\echo === 6. ZERO-entry transaction — must be REJECTED at COMMIT (min 2 entries) ===
BEGIN;
INSERT INTO transactions (id, stripe_event_id, stripe_object_id, event_type, occurred_at, memo)
VALUES ('00000000-0000-4000-8000-000000000003', 'evt_proof_zero_entries', 'ch_proof_zero',
        'charge.succeeded', now(), 'phase0 trigger proof: zero entries');
COMMIT;

\echo === 7. Balances (derived view) and the global invariant after the one committed transaction ===
SELECT name, kind, normal, balance_minor FROM account_balances ORDER BY name;
SELECT currency,
       SUM(CASE WHEN normal='debit'  THEN balance_minor ELSE 0 END) AS sum_debit_normal,
       SUM(CASE WHEN normal='credit' THEN balance_minor ELSE 0 END) AS sum_credit_normal
  FROM account_balances
 GROUP BY currency;

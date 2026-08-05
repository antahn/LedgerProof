-- 002_seed_accounts.sql — chart of accounts (LEDGERPROOF_BRIEF §4.3), USD.
BEGIN;

INSERT INTO accounts (name, kind, normal, currency) VALUES
  ('stripe_balance',     'asset',     'debit',  'USD'),
  ('bank',               'asset',     'debit',  'USD'),
  ('processing_fees',    'expense',   'debit',  'USD'),
  ('dispute_losses',     'expense',   'debit',  'USD'),
  ('refunds_contra',     'expense',   'debit',  'USD'),
  ('revenue',            'revenue',   'credit', 'USD'),
  ('customer_liability', 'liability', 'credit', 'USD');

COMMIT;

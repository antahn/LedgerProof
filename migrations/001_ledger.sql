-- 001_ledger.sql — the correctness core.
--
-- Four objects: ledger -> account -> transaction -> entry. One transaction,
-- at least two entries: one source of funds, one use.
--
-- Balances are DERIVED (a view), never stored. Append-only and per-transaction
-- balance are enforced BY THE DATABASE — triggers plus a least-privilege role —
-- not by application code.

BEGIN;

CREATE TYPE direction AS ENUM ('debit', 'credit');

CREATE TABLE accounts (
  id          BIGSERIAL PRIMARY KEY,
  name        TEXT      NOT NULL UNIQUE,
  kind        TEXT      NOT NULL CHECK (kind IN ('asset','expense','liability','equity','revenue')),
  normal      direction NOT NULL,
  currency    CHAR(3)   NOT NULL,
  -- asset/expense are debit-normal; liability/equity/revenue are credit-normal.
  CONSTRAINT normal_matches_kind CHECK (
    (kind IN ('asset','expense')                  AND normal = 'debit') OR
    (kind IN ('liability','equity','revenue')     AND normal = 'credit')
  )
);

CREATE TABLE transactions (
  id            UUID PRIMARY KEY,
  stripe_event_id  TEXT,          -- event.id
  stripe_object_id TEXT,          -- data.object.id
  event_type       TEXT,
  occurred_at   TIMESTAMPTZ NOT NULL,   -- from Stripe
  recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  memo          TEXT,
  -- Stripe's own documented dedupe key: event.id is NOT sufficient, because
  -- two distinct Event objects can be generated for the same state change.
  CONSTRAINT dedupe_event  UNIQUE (stripe_event_id),
  CONSTRAINT dedupe_object UNIQUE (event_type, stripe_object_id)
);

CREATE TABLE entries (
  id             BIGSERIAL PRIMARY KEY,
  transaction_id UUID      NOT NULL REFERENCES transactions(id),
  account_id     BIGINT    NOT NULL REFERENCES accounts(id),
  dir            direction NOT NULL,
  amount_minor   BIGINT    NOT NULL CHECK (amount_minor > 0),  -- minor units; never float
  currency       CHAR(3)   NOT NULL
);
CREATE INDEX ON entries (transaction_id);
CREATE INDEX ON entries (account_id);

-- Balances are derived, never stored. Sign is the product of entry direction
-- and account normal — it is not a property of the entry alone.
CREATE VIEW account_balances AS
SELECT a.id, a.name, a.kind, a.normal, a.currency,
       COALESCE(SUM(CASE WHEN e.dir = a.normal
                         THEN e.amount_minor ELSE -e.amount_minor END), 0) AS balance_minor
FROM accounts a LEFT JOIN entries e ON e.account_id = a.id
GROUP BY a.id, a.name, a.kind, a.normal, a.currency;

-- ---------------------------------------------------------------------------
-- Append-only enforcement. Corrections are new compensating transactions.
-- ---------------------------------------------------------------------------

CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'ledger is append-only: % on % is forbidden', TG_OP, TG_TABLE_NAME;
END $$;

CREATE TRIGGER entries_append_only
  BEFORE UPDATE OR DELETE ON entries
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CREATE TRIGGER transactions_append_only
  BEFORE UPDATE OR DELETE ON transactions
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- TRUNCATE does not fire UPDATE/DELETE triggers, so it needs its own —
-- otherwise "append-only" has a silent mass-delete hole.
CREATE TRIGGER entries_no_truncate
  BEFORE TRUNCATE ON entries
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CREATE TRIGGER transactions_no_truncate
  BEFORE TRUNCATE ON transactions
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------------------
-- The per-transaction invariant, enforced at COMMIT. Entries are inserted one
-- at a time, so a per-row check would fail spuriously; the deferred constraint
-- trigger runs the check once the transaction's entries are all in place,
-- per transaction, per currency.
-- ---------------------------------------------------------------------------

CREATE FUNCTION assert_txn_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE bad RECORD;
BEGIN
  SELECT currency,
         SUM(CASE WHEN dir='debit'  THEN amount_minor ELSE 0 END) AS dr,
         SUM(CASE WHEN dir='credit' THEN amount_minor ELSE 0 END) AS cr
    INTO bad
    FROM entries WHERE transaction_id = NEW.transaction_id
   GROUP BY currency
  HAVING SUM(CASE WHEN dir='debit' THEN amount_minor ELSE 0 END)
      <> SUM(CASE WHEN dir='credit' THEN amount_minor ELSE 0 END)
   LIMIT 1;

  IF FOUND THEN
    RAISE EXCEPTION 'unbalanced transaction % in %: debits=% credits=%',
      NEW.transaction_id, bad.currency, bad.dr, bad.cr;
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER entries_balanced
  AFTER INSERT ON entries
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION assert_txn_balanced();

-- Companion deferred trigger: every transaction needs at least two entries
-- (one source of funds, one use). Anchored on transactions INSERT so a
-- zero-entry transaction is caught too — with no entry rows, an entries-side
-- trigger never fires.
CREATE FUNCTION assert_txn_min_entries() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM entries WHERE transaction_id = NEW.id;
  IF n < 2 THEN
    RAISE EXCEPTION 'transaction % has % entries; a balanced transaction needs at least 2',
      NEW.id, n;
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER transactions_min_entries
  AFTER INSERT ON transactions
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION assert_txn_min_entries();

-- ---------------------------------------------------------------------------
-- Revoked grants: the application role physically lacks UPDATE/DELETE/TRUNCATE,
-- so a stray statement fails on privilege before it even reaches a trigger.
-- (Role is cluster-wide; guard so both databases can run this migration.)
-- 'ledger_app' password is a local-dev container credential, not a secret.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ledger_app') THEN
    CREATE ROLE ledger_app LOGIN PASSWORD 'ledger_app';
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO ledger_app;
GRANT SELECT ON accounts, transactions, entries, account_balances TO ledger_app;
GRANT INSERT ON transactions, entries TO ledger_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ledger_app;

COMMIT;

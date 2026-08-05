-- 003_stripe_events.sql — ingest event log + fast-path dedupe.
-- NOT part of the append-only ledger: status advances as the worker processes
-- the event, so ledger_app gets UPDATE here (and only here).
BEGIN;

CREATE TABLE stripe_events (
  id           TEXT PRIMARY KEY,            -- event.id
  event_type   TEXT NOT NULL,
  object_id    TEXT,
  payload      JSONB NOT NULL,
  received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  status       TEXT NOT NULL DEFAULT 'received'
               CHECK (status IN ('received','queued','processed','no_money_moved','failed')),
  CONSTRAINT stripe_events_object_dedupe UNIQUE (event_type, object_id)
);

GRANT SELECT, INSERT, UPDATE ON stripe_events TO ledger_app;

COMMIT;

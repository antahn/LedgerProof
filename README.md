# LedgerProof

A double-entry payments ledger built around one invariant: **for each currency,
the sum of debit-normal account balances equals the sum of credit-normal account
balances — always.** A mismatch means the system created or destroyed money out
of nothing. That invariant is enforced by the database itself (a deferred
constraint trigger that runs at `COMMIT`), not by application code, and the
ledger is append-only by trigger and by revoked grants: corrections are new
compensating transactions, never mutations. The point of the project is not the
Stripe integration — it is the **adversary**: a chaos proxy that deliberately
duplicates, reorders, delays, drops, tampers with, and 500s every webhook
delivery to find the bugs the happy path hides.

> Findings count: **the harness has not run yet.** [FINDINGS.md](FINDINGS.md)
> records only measured results; every number in this README traces to a run in
> `artifacts/`.

## Architecture

```
Stripe (test mode + test clocks)
   │  webhook delivery
   ▼
chaos-proxy  ──► duplicate │ reorder │ delay │ drop │ 500 │ tamper │ stale-ts │ concurrent-dup │ partial-write
   │
   ▼
ingest (FastAPI)   verify signature (raw body, v1 only, constant-time, 5-min tolerance)
   │               dedupe on event.id AND (event.type, data.object.id)
   │               return 200 IMMEDIATELY ──► enqueue
   ▼
worker (Celery)    map event ──► balanced transaction
   │               SERIALIZABLE; retry on 40001
   ▼
Postgres           append-only entries; balances are VIEWs; deferred constraint trigger
   │
   ├──► reconciler       pull Stripe balance_transactions, diff vs ledger
   ├──► invariant check  Σ debit-normal == Σ credit-normal, per currency
   └──► triage agent     proposes repairs, never applies
```

**Stack:** Python 3.12 / FastAPI / Postgres 17 / Redis / Celery, `stripe` SDK,
`pytest` + Hypothesis. Test mode only — no live keys, ever.

## Why each piece exists

Most "I used the Stripe API" projects are the Checkout quickstart plus a webhook
handler that assumes each event arrives exactly once, in order. Stripe's own
documentation says the opposite on both counts. All the interesting engineering
is in the failure modes.

| Feature | Skill it evidences | Who screens for it |
|---|---|---|
| Append-only ledger, balances as derived views | Data modeling under a correctness invariant; understanding why mutable balances are a bug | Stripe, Ramp (fintech core) |
| DB-enforced balance invariant (deferred constraint trigger) | Pushing correctness into the strongest available layer | Stripe, Databricks |
| Idempotency keys on every outbound `POST`, `Stripe-Should-Retry` honored, backoff **with jitter** | Exactly-once semantics under partial failure; the single most-written-about topic on Stripe's engineering blog | Stripe |
| Signature verification: v1-only, constant-time, 5-min tolerance, raw body | Security fundamentals that Stripe's docs call out explicitly as footguns | Stripe, Ramp |
| Order-independent, duplicate-tolerant handlers | Reading docs precisely — Stripe guarantees *neither* ordering *nor* exactly-once delivery | Stripe integration round |
| **Chaos proxy** | Adversarial thinking; building the thing that finds your own bugs | All three — this is the differentiator |
| Debugging your own harness's findings | The Stripe "bug bash" round, where a well-explained diagnosis beats a finished fix | Stripe |
| Test clocks in CI | Deterministic tests for time-dependent behavior; near-zero adoption in student work | Stripe, Databricks |
| GCRA rate limiter + tiered load shedding | Backpressure and graceful degradation | Stripe (4 limiter tiers), Databricks (operational rigor) |
| Reconciler diffing against Stripe's own `balance_transactions` | External reconciliation — the thing that catches what tests miss | Ramp, Stripe |
| Harness-labeled fault benchmark + model frontier | Production-grounded evals, not vibes; cost/latency/accuracy tradeoffs | Ramp, Databricks |
| Public write-up | Every hired-off-a-project case follows the same pattern: measurable artifact + public post | All three; Stripe's culture is explicitly writing-heavy |

## Running it

```powershell
docker compose up -d          # Postgres 17 + Redis 7
uv sync                       # Python 3.12 env + deps
uv run python scripts/migrate.py                 # apply migrations to ledgerproof
uv run python scripts/migrate.py --test          # ... and ledgerproof_test
uv run pytest                 # test suite
```

Secrets live in `.env` (never committed); see `.env.example` for the names.
Stripe **test mode only** — keys must start with `sk_test_`.

## Repository map

- `migrations/` — plain numbered SQL; the schema and its enforcement triggers
- `src/ledgerproof/ledger/` — posting, balances, the global invariant check
- `src/ledgerproof/stripe_io/` — manual signature verification, idempotent egress client, event→transaction mapping
- `src/ledgerproof/ingest/` — FastAPI webhook endpoint: verify, dedupe, enqueue, 200
- `src/ledgerproof/worker/` — Celery worker: SERIALIZABLE posting with 40001 retry
- `src/ledgerproof/recon/` — reconciler vs Stripe `balance_transactions`
- `src/ledgerproof/limits/` — GCRA limiter + tiered load shedding
- `src/ledgerproof/triage/` — LLM triage agent (proposes repairs as data; never writes)
- `harness/` — the chaos proxy, fault taxonomy, scenario generator, runner
- `artifacts/` — raw output of every run; never hand-edited
- `FINDINGS.md` — every bug the harness found: repro, diagnosis, fix
- `LEDGERPROOF_BRIEF.md` — the full build brief this project follows

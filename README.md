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

> **What the adversary found:** across 189 fault-injection scenarios, one
> critical bug — an unclean worker death stranded a payment where every
> recovery mechanism believed someone else had handled it, losing the money
> outright in 3 of 12 kill scenarios while the conservation invariant stayed
> green. Plus 5 more from a structured adversarial review and 1 from the first live
> webhook. The harness itself was wrong three times first, and those
> corrections are written down too. Full repro, diagnosis, and fix for each:
> [FINDINGS.md](FINDINGS.md). Every number here traces to a run in
> `artifacts/`.

> **What the benchmark found:** the harness labels its own scenarios, so 315
> of them became a fault-classification benchmark for free. Across 1,020
> batched calls, **Sonnet 5 matched Opus 5's perfect `acc@3`** at a fraction of
> the cost — though how large a fraction turns out to be mostly a caching
> artifact, and the honest normalised gap is ~1.3×, not 3.8×. Raising Sonnet's
> effort setting made it *worse* every time. And on 44 damaged ledgers, **no
> model proposed a repair on any of the 36 that were fixable** — every proposal
> in the study landed on the 8 where repair is provably impossible, which the
> cheapest configurations made confidently.

## Architecture

```
LIVE PATH                          HERMETIC PATH (chaos + benchmark)
Stripe test mode + test clocks     harness replayer ──► chaos-proxy
  real webhook delivery              duplicate │ reorder │ delay │ drop │ 500
                                     tamper │ stale-ts │ concurrent-dup │ …
                                     own signing secret, DB, Redis DB, port
          │                                       │
          └───────────────────┬───────────────────┘
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

Most Stripe integrations are a Checkout quickstart plus a webhook handler that
assumes each event arrives exactly once, in order. Stripe's own documentation
says the opposite on both counts. All the interesting engineering is in the
failure modes, so every component here exists to survive a specific one.

| Component | The failure it addresses |
|---|---|
| Append-only ledger, balances as derived views | A mutable balance column destroys the evidence of how it got there. Overwrite it once and drift becomes undetectable. |
| DB-enforced balance invariant (deferred constraint trigger) | Application-layer checks are bypassable by the next code path. The trigger runs at `COMMIT` for every writer, including `psql`. |
| Idempotency keys on every outbound `POST`, `Stripe-Should-Retry` honored, backoff with full jitter | A network timeout is indistinguishable from a success. Retrying without a key double-charges; retrying in lockstep stampedes. |
| Signature verification: v1-only, constant-time, 5-min tolerance, raw body | Accepting `v0`, comparing with `==`, ignoring the timestamp, or verifying re-serialized JSON each turn the check into decoration. |
| Order-independent, duplicate-tolerant handlers | Stripe guarantees neither ordering nor exactly-once delivery. Two events can describe one state change, and one event can arrive twice. |
| **Chaos proxy** | The happy path hides the bugs. This deliberately duplicates, reorders, delays, drops, tampers with, and 500s deliveries, and knows the ground truth for each. |
| Test clocks | Trials, renewals, and dunning are time-dependent, so they are normally tested by waiting or not at all. A frozen forward-only clock makes them deterministic. |
| GCRA rate limiter + tiered load shedding | Under overload, something must be dropped. Better to choose the order deliberately — test traffic, then reads, then writes, and the money path last. |
| Reconciler diffing against Stripe's `balance_transactions` | The invariant proves internal consistency, not agreement with reality. Only an external source catches a payment that never arrived. |
| Harness-labeled fault benchmark | The harness caused every scenario, so it knows the correct label for free — a real eval set instead of a hand-written one. |
| Triage agent that proposes but never applies | An agent near money should emit repairs as data. `approve_and_apply` is the only write path, and it refuses without a named human. |

The recurring theme, and the reason most of these exist: **money conservation
proves the books are internally consistent, it does not prove they match
reality.** Every serious bug found here lived in that gap.

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
- `FINDINGS.md` — every bug found: repro, diagnosis, fix

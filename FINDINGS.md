# FINDINGS

**This file records only measured results.**

Every bug listed here carries a minimal reproduction, a root-cause diagnosis in
prose, and the fix. Counts are truthful — if a category found zero breaks, that
is what this file says. Anything skipped, sampled, or capped is recorded under
Coverage notes, because silent truncation reads as "covered everything."

Findings are attributed to whatever actually found them. The chaos harness
(Phase 2) has **not run yet**; the section below records what a structured
adversarial code review found in the first implementation *before* the gate —
kept separate so harness results are never inflated by review results.

---

## Found by pre-gate adversarial review — 2026-08-04

Method: five reviewers examined the Phase-1 implementation against the brief's
specs (§4.2/§4.3/§5.1/§5.2/§5.3), one dimension each; every candidate finding
was then attacked by an independent skeptical verifier instructed to refute it,
with empirical demonstration required. Raw output (all 27 raw findings, the 5
confirmations, 5 refutations, 17 deferred):
[`artifacts/reviews/phase1_adversarial_review.json`](artifacts/reviews/phase1_adversarial_review.json).
All 104 Phase-1 tests were green while every one of these bugs existed — each
one lives precisely in the gap the test suite didn't cover.

### R1 (critical) — One real payment posts twice

- **Where:** `worker/handlers.py` (`HANDLED_EVENT_TYPES`)
- **Repro (demonstrated by the verifier):** deliver both events Stripe emits
  for a single $10.00 PaymentIntents payment — `charge.succeeded` (`ch_1`) and
  `payment_intent.succeeded` (`pi_1`). Result: `revenue=2000`,
  `processing_fees=118`, `stripe_balance=1882` — exactly double a
  1000-minor-unit payment.
- **Diagnosis:** both event types were handled and both map to the identical
  posting. Every dedupe key is *event*-scoped — `event.id`s differ, and
  `(event_type, object_id)` differs in **both** components
  (`charge.succeeded/ch_1` vs `payment_intent.succeeded/pi_1`) — so no
  constraint fires. The money-conservation invariant stays green because both
  sides doubled: conservation checks internal consistency, not agreement with
  reality. The brief's §4.3 puts the two event types on *one* mapping row
  because they are one money movement; treating them as two independently
  postable events is the bug.
- **Fix:** `payment_intent.succeeded` removed from `HANDLED_EVENT_TYPES`
  (§5.3: subscribe only to what you handle; `charge.succeeded` carries the
  money). The mapping retains PI support as a documented, unsubscribed
  alternate. Regression test delivers both events and asserts exactly one
  transaction.

### R2 (critical) — A failed enqueue permanently drops an event

- **Where:** `ingest/app.py` + `ingest/dedupe.py`
- **Repro (demonstrated):** delivery 1 with a broken queue → 500, but the
  dedupe row committed with status `queued`. Redelivery with a healthy queue →
  `200 {"status":"duplicate"}`, nothing enqueued, ever.
- **Diagnosis:** the dedupe record committed on its own connection *before*
  enqueue ran, conflating "recorded" with "queued". Stripe's retry — the
  mechanism that exists to heal exactly this — was answered `duplicate`, which
  tells Stripe to stop retrying an event that never reached the worker. No
  code path re-enqueued stuck rows. Missing transactional-outbox shape.
- **Fix:** rows insert as `received`; the status flips to `queued` only after
  a successful enqueue; a duplicate delivery that finds the existing row still
  in `received` re-enqueues it (the worker is idempotent, so double-enqueue is
  safe) before answering 200; enqueue failure returns an explicit 500 so
  Stripe retries. Regression test replays the exact demonstrated sequence.

### R3 (major) — The second partial refund silently vanishes

- **Where:** `stripe_io/mapping.py` (`charge.refunded` keying)
- **Repro (demonstrated):** refund $30 of a $100 charge, then $50 more.
  Refund #1 posted; refund #2 → `duplicate`; `refunds_contra` ends at 3000
  against a true 8000, invariant green, event log `processed`.
- **Diagnosis:** `charge.refunded` fires once per refund with `data.object` =
  *the charge*, so two partial refunds share the `(charge.refunded, ch_x)`
  dedupe pair. The `dedupe_object` constraint — built for Stripe's documented
  "two Events for the same state change" case — also swallowed a genuinely new
  state change. Keying dedupe by the *event's object* instead of the *object
  that moved money* is the root cause.
- **Fix:** a single money-movement identity function
  (`mapping.money_movement_object_id`) now feeds both the ingest dedupe and
  the transaction's `stripe_object_id`: for `charge.refunded` it is the
  newest refund's id (`re_…`), and the posted amount is that refund's own
  amount rather than the cumulative `amount_refunded`. Two partial refunds are
  two balanced transactions; replaying the same refund is still a duplicate.

### R4 (major) — Nothing tied an entry's currency to its account's

- **Where:** `migrations/001_ledger.sql` (schema gap)
- **Repro (demonstrated):** `post_transaction` accepted DR `stripe_balance` /
  CR `revenue` for 100000 minor units with `currency='EUR'`; the USD-labeled
  balances moved at face value and `invariant.check` stayed `ok=True` — EUR
  cents counted as USD cents with zero alarms.
- **Diagnosis:** the per-transaction balance trigger groups by *entry*
  currency while the balance view and global invariant group by *account*
  currency, and nothing reconciled the two groupings. Any non-USD event
  reaching the USD chart of accounts slid through the gap; the two groupings
  could also disagree in the other direction and report phantom drift.
- **Fix:** `migrations/004_hardening.sql` adds `UNIQUE (id, currency)` on
  accounts and a composite foreign key on entries
  `(account_id, currency) → accounts (id, currency)` — declarative, in the
  strongest available layer, no trigger required. Test posts a mismatched
  entry and asserts the database rejects it.

### R5 (major) — Worker exceptions were acked and forgotten

- **Where:** `worker/tasks.py`
- **Diagnosis:** `process_event` had no retry policy, and Celery acks a
  *raised* task by default even with `acks_late` (that setting only protects
  against process death, e.g. the PARTIAL_WRITE fault — not ordinary
  exceptions). A Postgres restart while draining a 50-event backlog would ack
  and drop all 50: not in the queue, not posted, rows stuck at `queued`,
  nothing marked `failed`. Fossil evidence: migration 003's CHECK constraint
  allowed a `'failed'` status that no code path ever wrote.
- **Fix:** bounded retries with exponential backoff + jitter on exception;
  when retries are exhausted the event's row is durably marked `failed` — a
  dead-letter signal the reconciler can act on — and the error re-raised.

## Found by the chaos harness

*(the harness has not run — Phase 2)*

## Coverage notes

- The review verified the **top 10** findings by severity; **17 lower-severity
  candidates were deferred unverified** (list in the artifacts JSON). Of
  those, nine were fixed alongside the confirmed five (send_task producer
  bug, non-event JSON crash, missing body-size cap, replay metric blind to
  cached-error replays, non-JSON 2xx handling, unclosed httpx pool,
  jitter-test weakness, SERIALIZABLE-untested, `ledger_app` privilege gaps);
  the rest are recorded there, not silently dropped.
- Five findings were **refuted** by verifiers (details in the artifacts JSON);
  refuted claims are not counted anywhere.

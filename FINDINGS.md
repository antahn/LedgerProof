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

## Found by the first live delivery — 2026-08-04

### R6 (major) — Real webhooks carry `balance_transaction: null`; the fetch path assumed at least an id

- **Where:** `worker/handlers.py` (missing-fee fetch path)
- **Repro:** the very first real webhook this system ever received.
  `stripe trigger charge.succeeded` → the delivered payload's
  `balance_transaction` was `null`, not an unexpanded id string — Stripe does
  not attach it until the charge settles into the balance. Raw evidence:
  [`artifacts/phase0_stripe_e2e.txt`](artifacts/phase0_stripe_e2e.txt).
- **Diagnosis:** `MissingFeeData` carried `balance_transaction_id=None` and
  the handler's fetch path required a non-None id, so it re-raised. Every
  synthetic fixture in the test suite had either an expanded object or an id
  string — the null-until-settlement shape exists only in production traffic.
  Two designed behaviors performed exactly as intended around the bug: the
  worker retried with jittered exponential backoff (3.4s → 6.8s → 15.5s → …,
  observed live), and on exhaustion durably marked the event `failed` (the R5
  dead-letter fix) with zero partial ledger writes — the event was recoverable,
  not lost.
- **Fix:** when the payload lacks even the balance-transaction id, the handler
  re-fetches the *charge* by the id it does have, with
  `expand[]=balance_transaction` (§5.3: fetch the missing object using the IDs
  on hand). If the re-fetched charge is still unsettled, re-raising is correct:
  the backoff retry waits out settlement rather than inventing a fee. The
  `failed` event was then re-driven from its stored payload through the fixed
  worker — idempotently — and posted with the true live numbers: gross 100,
  fee 33 (Stripe's real 2.9% + 30¢ test fee, fetched from the API), net 67;
  invariant 1100 == 1100. Regression tests cover both the refetch-and-post and
  the still-unsettled-raise paths.

## Found by the chaos harness — 2026-08-05

The harness delivers webhooks it signs itself, against its own database, Redis
queue, and port, so a run is hermetic and repeatable. Every scenario carries a
ground-truth `Expectation` derived from the same plan that drives the wire, and
break detection is a pure function of *plan vs. observed ledger* — it is never
passed the label, so a verdict cannot be contaminated by the answer key.

**First sweep — 189 scenarios (14 faults × 5 event kinds × 3 repeats): 27 broke,
3 of them with money missing outright.**
Raw: [`artifacts/chaos_20260805T074627Z.jsonl`](artifacts/chaos_20260805T074627Z.jsonl).

### H1 (critical) — An unclean worker death lost the payment

- **Where:** `ingest/app.py` (outbox recovery) with `worker/tasks.py` (broker config)
- **Repro:** `uv run python -m harness.runner --seed 7 --per-combo 3` on the
  code at commit `ed36a1f`. The `PARTIAL_WRITE` fault SIGKILLs the worker
  ~100 ms after delivery, then redelivers as Stripe would. Three of twelve kill
  scenarios ended with an empty ledger where entries were due — e.g.
  `partial_write-charge_succeeded-0001` expected
  `{stripe_balance +26533, processing_fees +823, revenue +27356}` and observed
  `{}`. As a unit test:
  `tests/integration/test_ingest.py::test_redelivery_recovers_a_queued_row_whose_worker_died`.
- **Diagnosis:** one word carrying more meaning than it had earned. A
  `stripe_events` row moves to `queued` when the queue is *told* about an
  event — never when the work *finishes*. The Phase-1 outbox fix (finding R2)
  only re-enqueued rows still in `received`, so a worker that died between
  "message enqueued" and "transaction committed" left the row parked at
  `queued`, which ingest read as *done*. Stripe's redelivery — the one
  mechanism built to heal exactly this — was answered `duplicate`, which tells
  Stripe to stop retrying. Nothing else recovered it either: `acks_late`
  promises redelivery only after the Redis broker's `visibility_timeout`, which
  defaults to **one hour**. So the money was not merely late, it was
  unreachable by every layer that could have saved it. Worth naming: the
  money-conservation invariant stayed **green** throughout. Conservation says
  the books are internally consistent, not that they match reality — a payment
  that never posts destroys nothing, it just never arrives. Only the harness's
  independent expectation of what *should* have posted could see it.
- **Fix:** a redelivery now re-enqueues any **non-terminal** row (`received` or
  `queued`) — neither proves the work was done, and the worker is idempotent
  end-to-end, so a redundant enqueue costs one no-op. `visibility_timeout`
  drops to 60 s so an unclean death self-heals without depending on Stripe at
  all. `failed` stays terminal deliberately: it is the exhausted-retries
  dead-letter the reconciler acts on, and auto-redriving it would erase the
  evidence instead of fixing anything.
- **After:** the identical sweep reports **0 of 189**
  ([`artifacts/chaos_20260805T082513Z.jsonl`](artifacts/chaos_20260805T082513Z.jsonl)),
  and wall-clock fell 565 s → 161 s, because those 15-second timeouts *were*
  the stranded rows.

### Three harness bugs, fixed before any number was trusted

The adversary was wrong before the product was. None of these are product
defects; all three would have made the measurements lie, in both directions.

- **The kill was too slow to ever land mid-write.** `kill_worker` took
  **922 ms**, because `uv run` remains a *parent* of the real Python process
  and reaching the child meant a `taskkill /T` tree walk — against a pipeline
  that commits in ~150 ms. Across 127 kills, **zero** landed in flight, so
  `PARTIAL_WRITE` was quietly proving "crash-then-redeliver is safe" while
  appearing to prove "no half-written transaction." Launching the worker as
  the venv interpreter directly makes `Popen.kill()` reach the transaction
  holder in **~0–16 ms**; the runner now records `kills_with_task_in_flight`
  per run (6 of 12 in the final sweep) so no run can over-claim atomicity.
  **H1 was only findable after this fix.** A slow adversary is a quiet one.
- **Quiescence was global, not per scenario.** One stranded row made *every
  later scenario* report `NOT_QUIESCENT`: 24 of the first sweep's 27 breaks
  were one bug echoing, drowning what those scenarios actually did. Now scoped
  to each scenario's own event ids.
- **`TRUNCATE_BODY` proved nothing about signatures.** Found by the mutation
  study below: with HMAC verification *entirely deleted*, that fault still read
  green, because half a JSON body is unparseable and ingest answered 400 from
  `json.loads` — the right status code from the wrong layer. Rejection faults
  now name the layer that must refuse them, and a refusal from anywhere else
  is a break (`WRONG_REJECTION_REASON`).

### Does the harness actually work? A mutation study

A zero-break sweep is worthless if the harness cannot go red, so the product
was deliberately broken eight ways, one at a time, each applied to a clean tree
and reverted before the next. Full evidence:
[`artifacts/mutation_check.json`](artifacts/mutation_check.json) and
[`artifacts/mutation/`](artifacts/mutation).

| # | Deliberate bug | Predicted | Result |
|---|---|---|---|
| M1 | `v0` signatures accepted | caught | caught — `ACCEPTED_BAD_DELIVERY` |
| M2 | timestamp tolerance ignored | caught | caught — 600 s-old replay posted |
| M3 | signature verification disabled | caught | caught by 3 of 4 signature faults; **`TRUNCATE_BODY` missed** |
| M4 | ingest dedupe removed, DB constraints intact | **not** caught | not caught — ledger held alone |
| M5 | dedupe removed at *both* layers | caught | caught — `DOUBLE_POST`, 8 transactions for 1 payment |
| M6 | order-dependence reintroduced | caught | caught — `LOST_EVENT` ×2 |
| M7 | balance trigger dropped + skewed amount | caught | caught — `INVARIANT_VIOLATION` |
| M8 | H1 regression | caught | caught — all 4 in-flight kills broke |

All eight predictions held; 15 of 51 scenarios broke. Three results are worth
more than the tally:

**M4 and M5 together show the dedupe is three layers deep, not two.** Removing
the ingest fast path changed real behavior — the worker received 13 tasks
across 4 scenarios instead of 4, so every redelivery it normally absorbs went
all the way through — and the ledger still recorded exactly one transaction per
scenario. Making a double-post even *expressible* required removing the ingest
fast path, dropping **both** `UNIQUE` constraints, *and* replacing the
deterministic `uuid5(event.id)` transaction id with a random `uuid4`. An
intermediate probe with the primary key left intact produced zero breaks: the
deterministic id is an independent third line of defense nobody designed as
one. A green result under M4 is a pass for defense-in-depth, not a blind spot —
with the authoritative layer intact there is no wrong ledger state for any
detector to see.

**M7 is the project's thesis under test.** With the database's guarantee
removed and a charge debiting the full gross instead of gross-minus-fee, the
harness reported money created from nothing — debit-normal 43808 vs
credit-normal 42545, a difference of 1263, *exactly the processing fee* — with
no fault injected at all.

**M6 is recorded as a weak injection.** Its `NONE` control also broke: with
only refund events delivered, no refund can post under that mutation
regardless of arrival order, so the bug is detectable without `REORDER` and
the fault's only contribution was doubling the lost-event count. The harness
caught it, but not for the reason the fault claims to test.

## Measured: deterministic lifecycle suites — 2026-08-07

Not a bug list — the Phase 3 result is a measurement. Three lifecycle suites
plus two negative controls run against the live Stripe sandbox on test clocks.
Raw output: [`artifacts/phase3_clocks_run.txt`](artifacts/phase3_clocks_run.txt);
metrics: [`artifacts/clocks_run.json`](artifacts/clocks_run.json).

| | Measured |
|---|---|
| Wall-clock runtime, all 5 suites | **180.0 s** |
| Simulated time covered | **124.2 days** |
| Trial | warned at `trial_end − 3d`, converted, charged 1500 minor |
| Renewal | 3 cycles, exactly one balanced transaction each, 7200 minor total |
| Dunning | **6 payment attempts**, terminal state `subscription.canceled` |

**The suites can fail.** A suite that cannot fail proves only that the API
responded, so two negative controls break the money path and require the
suites' own assertions to go red. The second calls
`clockkit.assert_cycles_posted` — the exact function the renewal suite passes
with — rather than a hand-written doomed assert, which would prove nothing
about the suite. The first drops the fee from a charge and requires the
database's deferred trigger to refuse the write, then verifies nothing
half-written survived.

Worth repeating from H1, because the control demonstrates it again in a
different setting: when the handler silently posts *nothing*, **the
money-conservation invariant stays green** while an entire billing cycle is
missing. Conservation proves internal consistency, not agreement with reality.
Only the explicit per-cycle count catches it — which is why the suites assert
it rather than leaning on the invariant.

## Measured: backpressure and shedding — 2026-08-08

Phase 4 is a measurement, not a bug hunt. Raw:
[`artifacts/shed_loadtest.json`](artifacts/shed_loadtest.json).

**Where each tier actually engages**, ramping pressure 0 → 1 over 200 samples.
The configured threshold is the *intent*; the engagement point is the
*behaviour*, and they differ because escalation requires two consecutive
agreeing samples:

| Tier | Threshold | Engages at | Releases at | Hysteresis band |
|---|---|---|---|---|
| `TEST_MODE` | 0.60 | **0.605** | 0.575 | 0.030 |
| `GET` | 0.75 | **0.755** | 0.725 | 0.030 |
| `POST` | 0.90 | **0.905** | 0.875 | 0.030 |
| `CRITICAL` | 0.98 | **0.985** | 0.955 | 0.030 |

Every tier releases *below* where it engaged. That gap is the anti-flap
deadband, and it is the number worth reading: a shedder that engaged and
released at the same pressure would oscillate, with every caller's retries
synchronised to the oscillation. Recovery is also deliberately slower than
escalation (5 calm samples vs 2 hot ones) — being slow to stop shedding costs a
little availability, being quick to stop shedding costs the whole system.

Across the ramp: 800 `TEST_MODE`, 1000 `GET`, 300 `POST` and 20 `CRITICAL`
requests were shed — the money path lost 20 out of 1005, and only above 0.985
pressure. **Dark launch dropped 0 while recording byte-identical verdicts**
(800/1000/300/20), which is the property that makes a dark launch worth
running. The shedder itself costs **~626,000 decisions/second**, comfortably
cheaper than the work it protects.

**The two limiter failure policies are asserted against each other** in one
test: an unreachable limiter allows (fail open — a safety device must not take
down what it guards), a contended lock denies (fail closed — that is not an
outage, it is a concurrent writer mid-update, and allowing would be the exact
double-admit the lock exists to prevent). A 32-thread race against one cold key
confirms the burst is never exceeded — the same unguarded read-modify-write the
chaos harness hunts in the ledger.

## Found while preparing to publish — 2026-08-07

### S1 (process) — A live signing secret reached a committed artifact

- **Where:** `artifacts/phase0_stripe_e2e.txt`, committed from `ed36a1f` onward.
- **How it happened:** the artifact was captured by tailing the `stripe listen`
  log, whose startup banner prints the endpoint signing secret. The capture was
  mechanical — "record the raw evidence" — and the banner came along with it.
  Reading the diff did not catch it, because the line looks like log output,
  which is exactly what it was.
- **Impact:** bounded but real. The leaked value was a **test-mode webhook
  signing secret**, not an API key — it can forge webhook signatures to an
  endpoint an attacker would also have to reach, and it touches no live money.
  No `sk_` key appeared in any commit, verified across all of history. The
  repository had never been pushed, so exposure stayed local.
- **Fix:** the secret was scrubbed from every commit
  (`git filter-branch` over all refs, reflog expired, objects pruned), with the
  artifact's surrounding evidence left intact. `scripts/scan_secrets.py` now
  matches credentials **by shape** — a real signing secret is 64 hex characters,
  a real key is a long base62 string — so it cannot be satisfied by an allowlist
  of "safe" paths, and it runs in CI before any test.
- **Rotation was attempted and is not available.** Re-authenticating the Stripe
  CLI (`stripe login`) issues a new CLI credential but returns the **same**
  signing secret: the `stripe listen` secret is account-scoped, not
  per-session, and the CLI exposes no command to rotate it. So the leaked value
  is still the live test-mode CLI signing secret, and this entry says so rather
  than claiming a rotation that did not happen. What it permits is forging a
  webhook signature to a listener an attacker must already be able to reach, in
  test mode, moving no real money — which is why the containment (scrub +
  scanner) is the substantive fix and rotation would have been belt-and-braces.
- **Why it is written down:** the brief's rule is that secrets never touch git,
  and the interesting part is not the rule but the failure mode. Careful review
  of a diff did not catch this; a check keyed to the shape of the thing did.

## Coverage notes

### Lifecycle suites (Phase 3)

- **Events reach the ledger through the worker's `handle_event`, not over
  HTTP.** The suites pull events from Stripe's Events API and feed the handled
  ones straight to the handler, so mapping, posting, and the invariant are
  exercised, but signature verification and the ingest endpoint are not. Those
  are covered end-to-end by the chaos harness and by the live `stripe listen`
  run recorded in `artifacts/phase0_stripe_e2e.txt`.
- **Event scoping is by object id**, so two suites sharing the sandbox cannot
  read each other's events. `assert_listing_is_scoped` explicitly proves the
  documented trap is real (an unscoped customer list does *not* return
  test-clock customers), because a suite built on an unscoped list would find
  nothing and pass vacuously.
- **Dunning's terminal state depends on account billing settings.** This
  sandbox is configured to cancel the subscription after retries are
  exhausted; an account set to "mark uncollectible" or "leave past due" would
  reach a different documented terminal state. The suite accepts any of the
  documented terminals and records which one occurred rather than asserting a
  single account-specific outcome.
- **`DELAY`-style time compression does not apply here** — these are real
  advances against real Stripe state; the 124.2 simulated days are genuine.
- **Hosted CI: green, lifecycle suites included.**
  [Run 31270280289](https://github.com/antahn/LedgerProof/actions/runs/31270280289)
  passed every step on Ubuntu — migrations, ruff, the secret scan, all 461
  unit/integration/**chaos** tests, and the **test-clock lifecycle suites**
  against the live sandbox. (The first run,
  [31269371028](https://github.com/antahn/LedgerProof/actions/runs/31269371028),
  correctly *skipped* the lifecycle step because the sandbox secret was not yet
  configured.) Worth noting: the chaos harness was developed on Windows against
  Windows-specific process control (`taskkill`, `--pool=solo`, `Popen.kill()`)
  and ran green on Linux unchanged.
- **The hosted run's clock metrics were not read back into this file.** GitHub
  returns 403 for unauthenticated log and artifact downloads, so what is
  verified here is the step's *conclusion* (success), not its numbers. The
  124.2-simulated-day / 180.0 s figures above remain the **local** measurement
  from `artifacts/phase3_clocks_run.txt`; the hosted equivalents are in that
  run's uploaded `ledgerproof-artifacts` bundle.

### Backpressure and observability (Phase 4)

- **The load test measures the shedding policy, not this laptop's sockets.** It
  drives the real `LoadShedder` against a synthetic pressure ramp rather than
  real HTTP, so the engagement points describe the algorithm. A network-level
  load test would mostly measure the test harness.
- **`TEST_MODE` tier membership is set by an explicit header**
  (`X-LedgerProof-Synthetic`), not by the event's `livemode` flag. Shedding must
  decide before the body is read, and this project is test-mode-only by rule, so
  `livemode` would classify every request identically. In a live deployment
  that is where `livemode` would be read.
- **Pressure is an injected callable, not a built-in signal.** Wiring it to real
  queue depth is a deployment decision; nothing in the repo currently feeds it
  in production, so the shedder is inert unless a caller supplies `pressure=`.
- **Grafana panels are unverified against a live Grafana.** The dashboard JSON
  parses and its queries are written against series the `/metrics` endpoint
  really exports (asserted by a test), but no Grafana instance has rendered it
  and no Prometheus has scraped the endpoint. The panels are a specification,
  not a screenshot.
- **Traces are exported nowhere by default.** `configure_tracing()` installs a
  provider with no exporter unless one is passed, deliberately: an import that
  opens a network connection is a landmine in tests. Cross-process propagation
  is verified by an in-memory exporter test, not against a collector.

### Chaos harness (Phase 2)

- **7 of 70 fault × event-kind combinations were deliberately not generated**,
  each recorded with a reason in every run's summary JSON. All seven are a
  fault whose ground truth is "exactly one transaction" crossed with
  `invoice.payment_failed`, which posts no transaction at all — the scenario
  could not fail no matter how broken the system was, and a green result would
  have been a lie.
- **`PARTIAL_WRITE` proves atomicity only for the kills that actually land
  mid-write.** In the final sweep that was **6 of 12**; the other 6 killed the
  worker outside a write window and prove the weaker (still real) property that
  an unclean death plus a redelivery yields exactly one transaction. The runner
  measures this per run rather than assuming it.
- **`DELAY` compresses time.** Its past-tolerance variant back-dates the
  signature instead of sleeping 300 s. Behaviour at ingest is identical; the
  run takes seconds instead of minutes.
- **`DROP` is measured against the harness's own record of what it delivered**,
  not against Stripe, because the hermetic loop uses synthetic events that have
  no Stripe counterpart. `reconcile_stripe` (diffing real
  `/v1/balance_transactions`) exists and is unit-tested, but no live
  reconciliation run has been recorded yet.
- The sweep uses one seed (`--seed 7`, 3 repeats per combination). It is not a
  randomized search over parameter space; a wider sweep is Phase 5's job.
- The mutation study covers 8 mutations, not an exhaustive mutation space, and
  each was run against a targeted subset of faults rather than the full sweep.

### Pre-gate review (Phase 1)

- The review verified the **top 10** findings by severity; **17 lower-severity
  candidates were deferred unverified** (list in the artifacts JSON). Of
  those, nine were fixed alongside the confirmed five (send_task producer
  bug, non-event JSON crash, missing body-size cap, replay metric blind to
  cached-error replays, non-JSON 2xx handling, unclosed httpx pool,
  jitter-test weakness, SERIALIZABLE-untested, `ledger_app` privilege gaps);
  the rest are recorded there, not silently dropped.
- Five findings were **refuted** by verifiers (details in the artifacts JSON);
  refuted claims are not counted anywhere.

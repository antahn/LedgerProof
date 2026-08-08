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
130 tests are green today
([`artifacts/phase1_test_run.txt`](artifacts/phase1_test_run.txt)); the
reviewers recorded 104 green at the time these bugs existed — their figure,
taken from their prose rather than a captured run. Either way, each bug lived
precisely in the gap the suite did not cover.

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
  worker — idempotently — and posted with the true live numbers: gross 1100,
  fee 63 (Stripe's real 2.9% + 30¢ test fee, fetched from the API), net 1037;
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
  code at commit `08cb5f6`. The `PARTIAL_WRITE` fault SIGKILLs the worker
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

- **The kill was too slow to ever land mid-write.** `kill_worker` reached the
  worker through a `taskkill /T` tree walk, because `uv run` remains a *parent*
  of the real Python process — measured at roughly a second, against a pipeline
  that commits in ~150 ms. The two pre-fix sweeps
  (`chaos_20260805T073233Z`, `073701Z`) each record
  `kills_with_task_in_flight: 0` of 12, so `PARTIAL_WRITE` was quietly proving
  "crash-then-redeliver is safe" while appearing to prove "no half-written
  transaction." Launching the worker as the venv interpreter directly lets
  `Popen.kill()` reach the transaction holder in single-digit milliseconds, and
  the runner now records the in-flight count per run so no run can over-claim
  atomicity. **H1 was only findable after this fix** — a slow adversary is a
  quiet one. *(The before/after latencies were measured interactively and are
  not preserved as an artifact; the in-flight counts in the sweep summaries
  are.)*
- **The in-flight kill rate is not reproducible, which bounds the claim.**
  Three runs at identical seed and parameters recorded **11/20, 2/20 and 5/20**
  kills landing inside a write. The 6-of-12 figure from the Phase-2 sweep is
  one draw, not a property of the system: `PARTIAL_WRITE` demonstrates
  atomicity only for the kills that happened to land mid-write in that run.
  Measuring the rate per run is what keeps the claim honest; it does not make
  it stable.
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
requests were shed — the money path lost 20 out of 1005, all at or above 0.985
pressure (the ramp steps 0.005, so those drops sit at 0.985/0.990/0.995/1.000). **Dark launch dropped 0 while recording byte-identical verdicts**
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

## Measured: the model frontier — 2026-08-08

**1,020 batched calls, 0 API errors, $17.78.** 170 cases per run (126 held-out
classification + 44 damaged-ledger repair), six runs: three models at default
effort plus an effort sweep on Sonnet 5. Raw:
`artifacts/sweep_20260808T194521Z.jsonl`, per-call usage in
`artifacts/llm_usage.jsonl`.

### The frontier

126 held-out cases across 13 fault classes plus a `NONE` baseline. Every column
is computed over those 126 classification cases only — the 44 repair cases are
excluded, cost included.

| Run | acc@1 | acc@3 | $/case (batched) | mean output tokens |
|---|---|---|---|---|
| **Opus 5** (default) | **0.976** | **1.000** | $0.0281 | 539 |
| **Sonnet 5** (default) | 0.905 | **1.000** | **$0.0073** | 742 |
| **Haiku 4.5** (default) | 0.825 | 0.841 | $0.0047 | 1,064 |

**Sonnet 5 matches Opus 5's perfect `acc@3`**, and reaches 93% of its top-choice
accuracy. If the task is "rank the likely faults for a human to confirm" — which
is what on-call triage actually is — the cheaper model is indistinguishable from
the expensive one on this benchmark. If the task is "answer once, unattended",
Opus 5's 7-point `acc@1` lead is the reason to pay more. Haiku is half Sonnet's
cost but drops 8 points of `acc@1` and, more tellingly, 16 points of `acc@3`:
its second and third guesses are much weaker, so it is the wrong choice for a
ranked-suggestions workflow.

**The `$/case` ratio is not a model property.** As-run, Sonnet is 26% of Opus's
cost — but that comparison rides almost entirely on cache asymmetry (see the
caching caveat below): Sonnet's default run read 99.2% of its prefix tokens from
cache and Opus's read 8.7%. Renormalised to a warm cache at the same list prices
and batch discount, Opus is $0.0095/case and Sonnet $0.0072 — a
**~1.3× ratio, not 3.8×**.
The accuracy ranking is unaffected. Quote the 26% only with the caveat attached.

### Higher effort never helped — the clearest result in the sweep

| Sonnet 5 effort | acc@1 | acc@3 | $/case (classification only) |
|---|---|---|---|
| default | **0.905** | **1.000** | **$0.0073** |
| `high` | 0.889 | 0.976 | $0.0192 |
| `medium` | 0.794 | 0.944 | $0.0174 |
| `low` | 0.794 | 0.952 | $0.0160 |

Every explicit effort setting was **worse** than leaving it alone, and tuning
effort downward to save money lost eleven points of accuracy. This is the result
the brief asks to be reported honestly if it appears: on this task, spending
more reasoning does not buy correctness.

**The cost half of that claim does not survive the caching caveat, and is
withdrawn.** The default run read 99.2% of its prefix from cache;
`low`/`medium`/`high` read 4.0% / 1.6% / 4.0%. Warm-cache-normalised $/case:
default $0.0072, `high` $0.0075, `medium` $0.0053, `low` **$0.0042**. Under equal caching `low` is
roughly 40% *cheaper* than default, so "more expensive" and "saved nothing"
invert. The accuracy finding stands on its own; the cost comparison between
effort levels in this table is not evidence.

### No model produced a single correct repair

**44 distinct damaged ledgers × 6 runs = 264 case-evaluations.** Zero correct
compensating transactions — from any model, at any effort.

The fixable stratum is **36 cases in every run** (the M5 double-posts); the
other 8 are the M7 unbalanced writes that no balanced transaction can repair.
Where a run answered fewer than 44 repair cases, the remainder returned no
parseable content — see the parse-failure caveat below.

| Run | correct repairs | repairs proposed on the 36 fixable | false repairs | claimed to fix the unfixable | repair cases answered |
|---|---|---|---|---|---|
| Opus 5 | 0 / 36 | **0** | 0 | 0 of 8 answered | 44 |
| Sonnet 5 (default) | 0 / 36 | **0** | 0 | — (0 of 8 answered) | 36 |
| Sonnet 5 `high` | 0 / 36 | **0** | 0 | — (0 of 8 answered) | 36 |
| Sonnet 5 `medium` | 0 / 36 | **0** | 1 | 1 of 2 answered | 38 |
| Sonnet 5 `low` | 0 / 36 | **0** | **8** | 8 of 8 | 44 |
| Haiku 4.5 | 0 / 36 | **0** | **7** | 8 of 8 | 44 |

**`repair_proposed` is false on all 36 M5 cases in all six runs.** No model
attempted a repair anywhere it was possible. The `0 / 36` column is zero because
nobody tried, not because anyone tried and failed — the models are not
distinguished on the repairable stratum at all. Every repair proposal in the
study landed on the 8 cases where repair is impossible, and the false-repair
counts are those same proposals, not an additional set.

Two rows need their denominators read carefully. Opus answered all 8 unfixable
cases and declined every one — the correct call, and the only run that
demonstrably made it. **Sonnet at default and `high` returned no parseable
content on any of the 8**, so they have no `/8` denominator; an earlier draft of
this file scored their silence as restraint, which the artifacts do not support.
Sonnet `medium` answered 2 of 8 and claimed to fix 1.

**Where a model expressed a view at all, lower capability and lower effort
correlated with confidently proposing impossible fixes** — the failure mode that
matters most for an agent allowed near money, and, together with the uniform
failure to act on the repairable stratum, the reason the human approval gate is
not optional.

### Where the models actually differ

Per-fault accuracy is near-ceiling and identical across all three models for
nine of the fourteen classes; two more (`DUPLICATE_OBJECT`, `PARTIAL_WRITE`)
differ by a single case. Three genuinely separate them:

| Fault | Opus 5 | Sonnet 5 | Haiku 4.5 |
|---|---|---|---|
| `REORDER` | **8/8** | 2/8 | 1/8 |
| `SLOW_LORIS` | 10/10 | 10/10 | **2/10** |
| `DELAY` | 7/10 | 6/10 | 5/10 |

`REORDER` is the discriminator: it requires noticing that the *later-generated*
event was recorded first, which means reading event ids as a sequence rather
than reading each delivery in isolation. Opus gets it every time; the others
almost never do.

### Caveats, stated rather than buried

- **`DELAY` → `STALE_TIMESTAMP` (9 of the pooled errors) is a label ambiguity,
  not a model failure.** A delay long enough to exceed the tolerance window
  *is* a stale signature at the point of rejection; the two labels describe the
  same observable. That combination should be one class, or `DELAY` should only
  ever be benign. Counted as errors here, so the reported accuracies are
  slightly pessimistic for every model.
- **Cost figures carry cache noise; accuracy figures do not.** Prefix cache-hit
  rates ranged from **1% to 99%** across runs, because a batch processes
  requests concurrently and a cache entry is only readable after the first
  response begins — so most of a batch can start cold. Sonnet's default run hit
  99% and Opus's hit 8%, which flatters Sonnet's $/case and penalises Opus's.
  The accuracy ranking is unaffected, but the cost axis should be read as
  approximate rather than a steady-state deployment figure.
- **28 of 1,020 responses (2.7%) returned no parseable content**, almost
  entirely Sonnet on the unbalanced-write cases — plausibly exhausting
  `max_tokens` reasoning about a repair that cannot exist. They are scored as
  wrong. Excluding them changes no ranking (Sonnet `medium` 0.794 → 0.820).
- One seed, one split, 126 classification cases. Differences of a point or two
  are not meaningful at this sample size; the Opus/Haiku gap and the effort
  result are.

## Measured: Phase 5 pilot — 2026-08-08

14 cases × 3 models, live calls, **$1.09 of a $15 cap**. Raw:
`artifacts/pilot_20260808T184220Z.jsonl`, usage in `artifacts/llm_usage.jsonl`.

| Model | acc@1 | acc@3 | p50 latency | $/case | mean output tokens |
|---|---|---|---|---|---|
| `claude-opus-5` | **0.64** | **0.86** | 10.9 s | $0.0368 | 1,167 |
| `claude-sonnet-5` | 0.43 | 0.71 | 12.0 s | $0.0335 | 1,928 |
| `claude-haiku-4-5` | 0.43 | 0.50 | 9.1 s | **$0.0075** | 1,258 |

**The prefix caching worked:** 13 of 14 calls hit cache on *all three* models
(the first writes it, the rest read). Before it was widened past Haiku 4.5's
4,096-token minimum, Haiku would have hit 0 of 14 and its measured cost — the
whole point of the frontier — would have been several times higher, with no
error to notice. The fix was worth making before spending, not after.

**Sonnet 5 is the worst value here, and the reason is visible in the tokens.**
It costs nearly as much as Opus 5 while matching Haiku's accuracy, because it
emits **65% more output tokens than Opus** (1,928 vs 1,167) and is wrong more
often for them. On this task more thinking is not better thinking. Haiku
delivers Sonnet's accuracy at **4.5× less cost**.

**Nobody proposed a correct repair — 0 across all three models.** Opus and
Sonnet proposed nothing at all (conservative, and therefore harmless). Haiku
produced 2 false repairs *and* claimed to fix 2 of 2 unbalanced writes — the
damage that provably cannot be repaired by any transaction the database would
accept. Confidently proposing an impossible fix is the worst available
behaviour, and it is the cheapest model doing it.

**Confidence is poorly calibrated**, most severely on the cheapest model:
Haiku reports 0.97 when right and 0.86 when wrong — almost no discrimination.
Opus: 0.77 vs 0.72. Sonnet is the best-calibrated (0.82 vs 0.53) despite being
less accurate, which is its one clear advantage in this run.

### P1 — Four more fault classes are unanswerable from the evidence given

This is a flaw in the benchmark, not a result about the models, and it is the
same mistake as the signature-attack collapse caught before the sweep — found
again only because the pilot's confusion matrix was read rather than its
headline number. Every one of these confusions occurred on **all three
models**, which is the signature of missing evidence rather than weak
reasoning:

| Confusion | Why no model could do better |
|---|---|
| `RESPOND_500` → `DUPLICATE` (×3) | The forced 500 is answered by the proxy *upstream* of ingest, so the recorded evidence shows two ordinary 200s — precisely `DUPLICATE`'s signature. The defining event never appears. |
| `CONCURRENT_DUPLICATE` → `DUPLICATE` (×3) | The stated giveaway is that deliveries *overlap in time*, but only `duration_ms` is rendered — never start timestamps. Overlap is literally not visible. |
| `PARTIAL_WRITE` → `DUPLICATE` (×3, incl. via `NONE`) | A kill followed by redelivery leaves two 200s and one transaction — again identical to `DUPLICATE`. The kill leaves no trace in the evidence. |
| `DELAY` → `NONE` (×3) | The in-tolerance variant delays ~250 ms and posts normally, which is indistinguishable from ordinary jitter at this resolution. |

Over half of all errors fall in these four classes. Reporting a frontier
built on them would be publishing the harness's blind spots as a fact about
Claude, so the full sweep was **not** run until the evidence was fixed.

**Fixed, and verified before spending again.** Each delivery now records when
it hit the wire relative to the event being generated, and what status the
*sender* was answered; the record carries whether the worker process restarted;
and the benign `DELAY` widened from 250 ms — indistinguishable from jitter — to
4 s. Measured on the regenerated set, the classes now separate:

Ranges below are over every scenario of each class in the regenerated set, not
single exemplars:

| Class | Distinguishing evidence, measured |
|---|---|
| `DELAY` | arrives **+4046–4141 ms** against `NONE`'s +47–141 ms |
| `CONCURRENT_DUPLICATE` | deliveries cluster: arrival **spread 0–17 ms** |
| `DUPLICATE` | deliveries stagger: **spread 172–672 ms** |
| `RESPOND_500` | `upstream=[500, 200]`: the forced error is finally visible |
| `PARTIAL_WRITE` | `worker_restarted: true` |
| `SLOW_LORIS` | handled for up to **2337 ms** while arriving promptly |

Two of those fields are **reconstructed by the harness, not observed on the
wire**: `upstream_status` is derived from the plan's `force_500` flag (in the
hermetic loop nothing actually answers 500 upstream), and `worker_restarted` is
true because the runner itself killed the worker. Both are signals a real
on-call engineer would have — Stripe's delivery log carries the upstream
status, a supervisor log carries the restart — so showing them is defensible,
but they are reconstructions rather than measurements, and the distinction
belongs in any description of what the model "sees".

One reclassification worth recording: `worker_restarted` had been on the
forbidden list, withheld as leakage. That was wrong. A process restart is a
line in any supervisor's log and the first thing an on-call engineer checks —
it is evidence, not the answer key. Withholding it made `PARTIAL_WRITE`
undiagnosable by construction rather than merely hard. Evidence that strongly
implies a fault is not leakage; only the label is.

## Found while preparing to publish — 2026-08-07

### S1 (process) — A live signing secret reached a committed artifact

- **Where:** `artifacts/phase0_stripe_e2e.txt`, committed from `08cb5f6` onward
  (the pre-scrub hash was `ed36a1f`, which the scrub itself invalidated).
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
- **Hosted CI: green, lifecycle suites included.** All seven workflow runs to
  date have succeeded; the latest on `HEAD` is
  [run 31276225439](https://github.com/antahn/LedgerProof/actions/runs/31276225439),
  which passed every step on Ubuntu — migrations, ruff, the secret scan, the
  full non-clock suite, and the **test-clock lifecycle suites** against the
  live sandbox. (The first run,
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
- **The wall-clock figure is not stable, and re-running the suite proved it.**
  The full test suite was re-run during Phase 6 with Stripe credentials present,
  which exercised the five live clock tests again; that run took **296.7 s**
  against the recorded 180.0 s. The simulated span (124.2 days) is deterministic
  and unchanged — it is a property of the test clocks, not the network. Only the
  wall-clock number moves, and it moves by 65%. `artifacts/clocks_run.json` was
  restored to the recorded run rather than silently overwritten by the re-run;
  the re-run's value is recorded here instead. Read "180.0 s" as one draw from a
  latency-dependent distribution, not a benchmark.

### Found by auditing this file against its own artifacts (Phase 6)

Before the write-up was drafted, every numeric claim here was recomputed from
the raw JSONL rather than from the summary files written alongside it. Most
held exactly. These did not, and are corrected above:

- The frontier table's `$/case` and mean-output columns were computed over all
  170 cases while the table was labelled "classification only" (126). Opus 5's
  classification-only mean output is **539** tokens, not 742 — 742 was Sonnet's
  figure. Corrected, which *improves* the headline: Sonnet reaches Opus's
  `acc@3` at **26%** of the cost, not 32%.
- The repair table mixed denominators. The fixable stratum is **36 in every
  run**; two rows showed 44 and 42, which were answered-case counts attached to
  the wrong column.
- Several narrative numbers had **no artifact behind them** — a 922 ms kill
  latency, "127 kills", a live backoff sequence, a 2,778-token prefix, and a CI
  test count. They were real observations made interactively, but a number in
  this file is supposed to trace to a committed artifact. They are now either
  removed, softened to what the artifacts support, or explicitly marked as
  unpreserved.

### Found by auditing the write-up draft — a second pass (Phase 6)

The draft of `writeup/POST.md` was audited against source and raw artifacts by
three independent adversarial reviewers, and every finding below was then
recomputed by hand before being applied. **The audit above was not sufficient**;
three of these change a claim rather than a digit.

- **The "classification only" fix was itself incomplete.** Only Opus's
  mean-output cell was recomputed; Sonnet's and Haiku's were left at their
  all-170 values (1,073 and 1,094) under a heading that says otherwise. The
  classification-only figures are **742.20** and **1,063.90**. Corrected above.
  The `$/case` column was correct.
- **The 26%-of-cost headline is dominated by cache asymmetry.** This file
  already carried the caveat 80 lines away from the claim; the claim is now
  stated with the correction attached (~1.3× normalised, not 3.8×). The same
  artifact inverts the effort table's cost comparison, which is withdrawn.
- **"No model produced a correct repair" understated the result.** No model
  *proposed* a repair on any of the 36 fixable cases, in any of the six runs —
  all 17 proposals in the study landed on the 8 unfixable cases. The `0 / 36`
  column was zero because nobody tried. Verified against `repair_proposed` in
  `sweep_20260808T194521Z_summary.json` (0/0/0/0/8/8 with 8/8/1 falling on the
  unfixable stratum).
- **"Opus 5 and Sonnet 5 at default proposed nothing — the safe answer" was
  unsupported for Sonnet.** Sonnet at default and `high` returned no parseable
  content on all 8 unfixable cases; silence from a truncated response was being
  scored as restraint. Only Opus demonstrably declined.
- The R6 narrative recorded the re-driven live event as "gross 100, fee 33, net
  67" in the same sentence as "invariant 1100 == 1100", which contradicts
  itself. `artifacts/phase0_stripe_e2e.txt` records
  `{'processing_fees': 63, 'revenue': 1100, 'stripe_balance': 1037}`. The
  written figures were the fee arithmetic for a 100-minor charge that never
  happened. Corrected above.
- Smaller corrections applied to the draft only: "24 of 27 breaks" is 24 of 27
  breaking *scenarios* (33 break records); one of M8's four in-flight kills
  stranded the outbox row rather than losing money; `CONCURRENT_DUPLICATE` did
  not fail on all three pilot models (Opus got both cases); the `DROP` exclusion
  from the 7 ungenerated combinations has the opposite rationale to the other
  six; the two-entry exhaustiveness proof is exhaustive over account pairs at a
  fixed amount, with a property test covering amounts; the 32-thread limiter
  race proves no over-admit, not that the burst is admitted; and the shedder's
  20 drops occur *at and above* 0.985, not above it.
- **Reproducibility defects in the draft's own commands**, found by running
  them: a `\` line continuation that PowerShell passes to pytest as a path
  (collection then hangs at the drive root rather than erroring), and a `/tmp`
  output path that resolves to three different locations across shells and
  silently creates `C:\tmp\` on Windows. Both fixed; the draft now also shows
  how to read the evidence out of the JSONL, and warns that the in-flight kill
  lands in roughly five runs of nine.
- "Ten of fourteen classes identical" was nine. "Roughly a third of all errors"
  understated its own point — the four unanswerable classes accounted for
  **over half**.
- Three commit hashes cited as reproduction anchors (`ed36a1f`, `bf8460e`) no
  longer resolve: the `filter-branch` scrub that removed the leaked secret
  rewrote every hash in the history. Corrected to their surviving equivalents.
  A history rewrite invalidates every SHA anyone has written down, including
  the ones inside artifacts — `artifacts/mutation_check.json` still records the
  dead `bf8460e` and cannot be corrected without editing an artifact, which the
  project's own rules forbid.

Further limitations the audit surfaced that this file had not admitted:

- **The shedder is not wired into the shipped app.** `ingest/server.py`'s
  `build_app()` — the documented production factory — never passes `pressure=`,
  so the deployed application never sheds. Calling that "a deployment decision"
  understated it.
- **`reconcile_stripe` has never run against Stripe.** Every one of its call
  sites is a test using a stub. The live-reconciliation half of the reconciler
  is unexercised.
- **The benchmark set is selected by filename.** `run_sweep.py` picks
  `max(glob("chaos_*.jsonl"))`, so any future chaos run silently redefines what
  "the benchmark" means. No hash or pin is recorded.
- **`artifacts/bench_design.json` describes a superseded set.** It was computed
  against `chaos_20260808T181504Z.jsonl`, generated *before* the evidence
  fixes; the sweep ran against `chaos_20260808T193114Z.jsonl`. Class balance and
  split sizes are identical, so the design conclusions stand, but its token
  counts and sample prompt do not describe what was measured.
- **The Grafana panel test proves only that metric names are registered.** At
  zero observations the `/metrics` endpoint emits `# HELP`/`# TYPE` lines but
  no `ledgerproof_ingest_seconds_bucket` series — which both latency panels
  query.
- **The constant-time signature comparison has no test.** It holds by
  construction (`hmac.compare_digest` inside a loop with no early exit); a
  timing assertion would be flaky, so it is reviewed rather than tested.

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

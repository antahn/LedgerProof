"""Prompt assembly for the triage agent (Phase 5).

Two hard requirements meet here, and they pull in the same direction.

**No leakage.** The agent sees only what an on-call engineer would have: the
ledger diff, the event log as ingest recorded it, and the source files that
define the system. It must never see the injected fault, the harness's params,
the scenario id, the expectation, or the runner's verdict. That is enforced
structurally — `build_case` takes an explicit allowlist of fields off the
artifact record rather than passing the record through and deleting keys, so a
new field added to the runner's output cannot silently appear in a prompt. A
test asserts the rendered prompt contains none of the forbidden values.

**Cacheable prefix.** The stable half (task, taxonomy, chart of accounts,
schema notes) goes in `system` with a cache breakpoint on the last block; the
per-scenario evidence goes in the user turn after it. Nothing volatile — no
timestamp, uuid, scenario id, or model name — may appear in the system prefix,
because caching is a prefix match and one changed byte invalidates everything
after it.

The prefix is deliberately padded past **4096 tokens**: that is Haiku 4.5's
minimum cacheable prefix (Opus 5 needs only 512, Sonnet 5 needs 1024). A prefix
sized for Opus would silently fail to cache on Haiku — no error, just
`cache_read_input_tokens: 0` and full-price input on every call, which would
inflate the cheapest model's measured cost and corrupt the cost comparison the
whole frontier plot rests on.
"""

from __future__ import annotations

from typing import Any

# Fields of a harness artifact record that may reach the agent. Anything not
# listed is withheld — including fields that do not exist yet.
#
# `worker_restarted` is here, not in FORBIDDEN_FIELDS, after the pilot showed
# PARTIAL_WRITE was unanswerable without it: a kill plus a redelivery leaves
# two 200s and one transaction, which is byte-identical to DUPLICATE. A worker
# process restarting is not the label — it is a line in any process
# supervisor's log and the first thing an on-call engineer checks. Withholding
# it made the class undiagnosable by construction rather than hard.
OBSERVABLE_FIELDS = (
    "ledger_diff",
    "invariant_after",
    "event_log",
    "deliveries",
    "worker_restarted",
)

# Fields that would give the answer away. Named explicitly so the leakage test
# can assert on them rather than on a vague notion of "the label".
FORBIDDEN_FIELDS = (
    "fault",
    "params",
    "scenario_id",
    "label",
    "expected",
    "breaks",
    "recon",
    "kinds",
    # Whether the kill LANDED inside a write is a harness measurement of its
    # own fault, not an operational signal — that stays hidden.
    "killed_with_task_in_flight",
    "redelivered_after_kill",
    "ledger_reset",
    "accepted_rejected",
    "wrong_rejection_reason",
    "quiescent",
)

TASK = """\
You are diagnosing a payments ledger that ingests Stripe webhooks.

Something happened to one webhook delivery on its way into the system, and you
are being shown what the system recorded afterwards. Your job is to name what
happened, explain how the evidence supports that, and — only if money is
actually missing or duplicated — propose a compensating transaction that
corrects the books.

You will not be told what happened. Reason from the evidence.
"""

ARCHITECTURE = """\
## How the system works

Stripe delivers a signed webhook over HTTP to an ingest endpoint. Ingest does
exactly four things and then answers immediately:

1. Reads the RAW request body.
2. Verifies the `Stripe-Signature` header: HMAC-SHA256 over `f"{t}.{body}"`
   keyed with the endpoint secret. Only the `v1` scheme is accepted (`v0` is a
   fake test-only signature and accepting it would be a downgrade attack), the
   comparison is constant-time, and the timestamp `t` must be within 300
   seconds of now. Any failure is a 400 with `{"error": "invalid signature"}`.
   A body over 512 KiB is refused first, with `{"error": "body too large"}`;
   correctly signed JSON that is not an event envelope is refused with
   `{"error": "not an event"}`; unparseable JSON is `{"error": "invalid JSON body"}`.
3. Deduplicates on BOTH documented keys: `event.id`, and
   `(event.type, money-movement object id)`. Stripe documents that two distinct
   Event objects can be generated for the same underlying state change, so
   `event.id` alone is insufficient. A duplicate is answered 200 without
   creating a second event-log row.
4. Enqueues the event and answers 200.

A worker then maps the event to a balanced double-entry transaction and posts
it at SERIALIZABLE isolation. The ledger is append-only: corrections are new
compensating transactions, never edits.

### The event log

Every accepted delivery creates one `stripe_events` row, whose `status` moves:

- `received` — recorded, but the enqueue has not yet succeeded.
- `queued` — handed to the queue. This does NOT mean the work finished.
- `processed` — the worker finished and posted (or deduplicated) it.
- `no_money_moved` — the event was handled but moves no money.
- `failed` — the worker exhausted its retries. This is a dead-letter marker.

A delivery that is refused at ingest creates NO row at all.
"""

CHART_OF_ACCOUNTS = """\
## Chart of accounts (USD)

| Account | Kind | Normal |
|---|---|---|
| `stripe_balance` | asset | debit |
| `bank` | asset | debit |
| `processing_fees` | expense | debit |
| `dispute_losses` | expense | debit |
| `refunds_contra` | expense | debit |
| `revenue` | revenue | credit |
| `customer_liability` | liability | credit |

A debit adds to a debit-normal account and subtracts from a credit-normal one;
a credit does the inverse. Balances shown to you are already signed this way,
so a positive `revenue` figure means revenue increased.

## Event mapping (each row balances)

| Stripe event | Debits | Credits |
|---|---|---|
| `charge.succeeded` | `stripe_balance` (gross - fee), `processing_fees` (fee) | `revenue` (gross) |
| `charge.refunded` | `refunds_contra` (refund amount) | `stripe_balance` (refund amount) |
| `charge.dispute.created` | `dispute_losses` (amount), `processing_fees` (dispute fee) | `stripe_balance` (amount + fee) |
| `payout.paid` | `bank` (amount) | `stripe_balance` (amount) |
| `invoice.payment_failed` | *no money moves* — an event-log row, never a transaction | — |
"""

TAXONOMY = """\
## The faults that can occur

Exactly one of these applies to each case. Several look similar in the ledger
and are told apart only by the event log and the HTTP responses.

| Fault | What happened on the wire | Signature in the evidence |
|---|---|---|
| `NONE` | Delivered normally, once | One delivery, 200, one event-log row, books moved by exactly the mapped amount |
| `DUPLICATE` | The same event delivered N times in a row | N deliveries all 200, ONE event-log row, books moved once |
| `DUPLICATE_OBJECT` | Two different `event.id`s for the same money movement | Two deliveries, both 200, one row, books moved once — the second key caught it |
| `CONCURRENT_DUPLICATE` | The same event on N parallel connections at once | Like DUPLICATE, but deliveries overlap in time rather than running in sequence |
| `REORDER` | Two related events released out of generation order | Both post; the later-generated event was recorded first |
| `DELAY` | Latency injected before delivery | One delivery, high duration; posts normally if the signature is still fresh, refused as stale if not |
| `DROP` | Never delivered at all | NO deliveries, NO event-log row, books unchanged — visible only by comparing against what Stripe says should exist |
| `RESPOND_500` | Ingest was forced to answer 500, then Stripe retried | Two deliveries of the same body; books moved exactly once |
| `TAMPER_BODY` | One byte flipped after signing | One delivery, 400 invalid signature, no row, books unchanged |
| `TRUNCATE_BODY` | Body cut short after signing | One delivery, 400 invalid signature, no row, books unchanged |
| `STALE_TIMESTAMP` | Signed with a back-dated timestamp | One delivery, 400 invalid signature, no row, books unchanged |
| `DOWNGRADE_SCHEME` | Signed with `v0` only | One delivery, 400 invalid signature, no row, books unchanged |
| `PARTIAL_WRITE` | The worker was killed mid-transaction, then the event was redelivered | Deliveries answered 200; the ledger shows either the whole transaction or none of it, never half |
| `SLOW_LORIS` | The body was dribbled slowly to hold the connection open | One delivery with a long duration; ingest stayed responsive and the event posted |

The four signature attacks (`TAMPER_BODY`, `TRUNCATE_BODY`, `STALE_TIMESTAMP`,
`DOWNGRADE_SCHEME`) are deliberately hard to tell apart: all four produce one
400 and no ledger movement. Use the response body and the delivery shape.
`DELAY` past the tolerance window also produces a stale-signature 400 — the
distinguishing evidence is the delivery's duration.
"""

TELLING_THEM_APART = """\
## Distinguishing the cases that look alike

### The four signature attacks

All four are answered `400 {"error": "invalid signature"}`, create no event-log
row, and move no money. The ledger cannot separate them; the request can. For
each refused delivery you are given the `Stripe-Signature` header that was
sent, the request body's size in bytes, and whether that body still parses as
JSON. A well-formed header looks like `t=<unix seconds>,v1=<64 hex chars>`.

| Evidence | Fault |
|---|---|
| Header carries a `v1=` signature, `t` is recent, body parses as JSON | `TAMPER_BODY` — bytes were altered after signing, so the digest no longer matches, but the payload is still structurally intact |
| Body does NOT parse as JSON, and is markedly smaller than a comparable payload | `TRUNCATE_BODY` — the body was cut, which breaks both the digest and the JSON |
| Header's `t` is far in the past (hundreds of seconds or more before the others) | `STALE_TIMESTAMP` — the payload is intact and the digest is correct for that `t`, but the timestamp fails the 300-second tolerance |
| Header has NO `v1=` item at all — only `v0=` | `DOWNGRADE_SCHEME` — a `v0` signature is a fake test-only scheme, and accepting it would be a downgrade attack, so it is ignored and the header counts as unsigned |

Read the header before guessing. The distinguishing evidence is usually one
field: a missing `v1=`, an old `t`, a body that no longer parses.

### `DELAY` versus a stale-signature rejection

A delayed delivery whose signature is still inside the tolerance window posts
normally — one 200, one row, books move — and looks like `NONE` apart from the
delivery's `duration_ms`, which will be conspicuously large. A delay long
enough to push the signature past tolerance is refused as stale. If you see a
400 for an old `t` AND a large `duration_ms`, prefer `DELAY`; if the timestamp
is old but the request arrived promptly, prefer `STALE_TIMESTAMP`.

### `SLOW_LORIS` versus `DELAY` — two different clocks

Each delivery carries two timings, and they mean different things:

- **`arrived +N ms`** — how long after the event was generated the request
  reached ingest. Large here means the request was held back before it was
  sent: that is `DELAY`.
- **`after N ms`** — how long ingest spent handling the request once it
  arrived. Large here means the body itself came in slowly: that is
  `SLOW_LORIS`.

So a delivery that arrives late and is handled fast is `DELAY`; one that
arrives promptly and takes seconds to handle is `SLOW_LORIS`. Both end with
the event posted and the books correct.

### Ordinary deliveries arrive within a few hundred milliseconds

`NONE` and the duplicate faults all arrive promptly. A single delivery that
arrives seconds after the event was generated is not a normal delivery, even
if everything else about it looks routine.

### `CONCURRENT_DUPLICATE` versus `DUPLICATE` — read the arrival offsets

Both deliver the same event several times and both end with one transaction.
The difference is visible in the `arrived +N ms` values:

- `DUPLICATE` sends them **one after another**, so the offsets step apart —
  each delivery starts after the previous one finished.
- `CONCURRENT_DUPLICATE` sends them **all at once**, so the offsets cluster
  within a few milliseconds of each other. That overlap is the whole point of
  the fault: it is the one that races the ledger's read-modify-write.

### `RESPOND_500` — look at what the SENDER was told

Ingest may record an ordinary 200 while the sender was answered a 500 by
something in front of it. When a delivery notes that the sender was answered
HTTP 500 and a later delivery of the same event succeeds, that is a retry
provoked by a server error, not a duplicate: `RESPOND_500`. Without that line
the two are indistinguishable.

### `PARTIAL_WRITE` — the worker restart is the signal

A killed worker plus a redelivery produces two 200s and one transaction, which
is exactly what `DUPLICATE` produces. The distinguishing evidence is the
infrastructure line: **worker process restarted: yes**. If the worker restarted
during the window and the event was delivered more than once, prefer
`PARTIAL_WRITE`. If it did not restart, the fault is one of the duplicates.

### The three duplicate faults

All three end with the books moved exactly once, because dedupe works. What
separates them is the shape of the deliveries and the ids involved:

- `DUPLICATE` — the same `event.id`, delivered several times one after another.
  Later responses say `duplicate` or `queued`; only one event-log row exists.
- `DUPLICATE_OBJECT` — two DIFFERENT `event.id`s that refer to the same money
  movement. The second is caught by the `(event.type, object id)` key rather
  than by the id, and it is the more interesting of the two because it proves
  the second dedupe key is doing real work.
- `CONCURRENT_DUPLICATE` — the same event on several connections at once. The
  giveaway is timing: deliveries overlap rather than running in sequence, so
  their durations are similar and they do not stack end to end.

### `RESPOND_500` versus `DUPLICATE`

Both show the same event delivered more than once and posted once. The
difference is that `RESPOND_500` involves a delivery that was answered with a
server error before the retry succeeded. If every delivery was answered 200,
it is a duplicate, not a retry.

### `PARTIAL_WRITE`

The worker was killed part-way through. The signature is what you do NOT see: a
half-written transaction. Either the whole balanced transaction is present or
none of it is, because the write was inside a database transaction that rolled
back. Deliveries are answered 200, and the event may have been redelivered.
"""

REPAIRS = """\
## Constructing a compensating transaction

Most cases need no repair. Propose one only when money is genuinely missing or
duplicated — not when a delivery was correctly refused, and not when dedupe
correctly absorbed a duplicate.

The ledger is append-only, so a correction is a NEW transaction that moves the
books back, never an edit or a deletion. To reverse a posting, post its mirror:
every debit becomes a credit of the same amount, and every credit becomes a
debit.

For example, if a `charge.succeeded` for a gross of 1000 with a fee of 59 was
posted twice, the books show `stripe_balance +1882`, `processing_fees +118`,
`revenue +2000` where they should show `+941`, `+59`, `+1000`. The repair
reverses exactly one copy:

- credit `stripe_balance` 941
- credit `processing_fees` 59
- debit `revenue` 1000

Debits total 1000, credits total 1000, so it balances and the database will
accept it. Getting this wrong in a way that still balances — for instance
crediting `bank` instead of `stripe_balance` — produces books that pass every
automated check while being wrong, which is worse than proposing nothing.

If an event never posted and should have, the repair is the posting the mapping
table prescribes for that event type, using the amounts visible in the
evidence. If you cannot determine the amounts from the evidence, say so in
`root_cause` and propose no repair rather than guessing figures.

## Why money conservation can look healthy while the books are wrong

For each currency, the sum of debit-normal balances always equals the sum of
credit-normal balances, because every transaction is balanced before it is
accepted. That check proves the books are internally consistent. It does NOT
prove they match reality: an event that never posted at all destroys nothing,
so conservation still holds while the money is simply missing. Treat
"conservation holds" as weak evidence — it rules out a half-written
transaction, and rules out nothing else.
"""

ANSWERING = """\
## What to return

- `fault_class`: your single best answer.
- `alternate_fault_classes`: up to two more, most likely first. When several
  faults produce identical evidence, list the others here — it costs nothing to
  be honest about ambiguity, and a confident wrong answer is worse than a
  ranked one.
- `confidence`: your calibrated probability that `fault_class` is right. If the
  evidence genuinely cannot separate four possibilities, roughly 0.25 is the
  honest answer, not 0.9.
- `root_cause`: cite the specific evidence — event ids, HTTP statuses, row
  statuses, balance figures. "It looks like a duplicate" is not a diagnosis;
  "three deliveries of evt_x all answered 200 while only one event-log row
  exists and revenue moved once" is.
- `money_is_missing`: true ONLY if the ledger is materially wrong. A delivery
  that was correctly refused moved no money and needs no repair; neither does a
  duplicate the system correctly absorbed. Most cases need no repair.
- `proposed_repair`: when and only when money is missing, a balanced set of
  entries that corrects it. Debits must equal credits per currency, and there
  must be at least two entries — the database enforces both and will reject
  anything else. Never propose editing or deleting existing rows; the ledger is
  append-only.

You are proposing, not acting. Nothing you return is applied automatically: a
human reviews every repair before it is posted.
"""


def system_prefix() -> list[dict[str, Any]]:
    """The stable, cacheable half of the prompt.

    Returned as content blocks with a cache breakpoint on the last one. Must be
    byte-identical across every request in a sweep — no timestamps, ids, or
    model names — or the cache never hits.
    """
    return [
        {"type": "text", "text": TASK},
        {"type": "text", "text": ARCHITECTURE},
        {"type": "text", "text": CHART_OF_ACCOUNTS},
        {"type": "text", "text": TAXONOMY},
        {"type": "text", "text": TELLING_THEM_APART},
        {"type": "text", "text": REPAIRS},
        {
            "type": "text",
            "text": ANSWERING,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def build_case(record: dict[str, Any]) -> dict[str, Any]:
    """Extract ONLY the observable evidence from a harness artifact record.

    Allowlist, not denylist: a field the runner adds later cannot leak into a
    prompt by default, because it simply is not copied.
    """
    deliveries = [
        {
            "http_status": d.get("status_code"),
            "response_body": d.get("body", ""),
            "duration_ms": d.get("duration_ms"),
            "error": d.get("error"),
            # The request side: what an engineer would pull from the request
            # log. Without it the four signature attacks are indistinguishable.
            "signature_header": d.get("request_signature_header"),
            "body_bytes": d.get("request_body_bytes"),
            "body_parses_as_json": d.get("request_body_parses_as_json"),
            "arrived_after_ms": d.get("arrived_after_ms"),
            "upstream_status": d.get("upstream_status"),
        }
        for d in record.get("deliveries", [])
    ]
    return {
        "ledger_diff": dict(record.get("ledger_diff") or {}),
        "invariant_holds": bool(record.get("invariant_after", True)),
        "event_log": list(record.get("event_log") or []),
        "deliveries": deliveries,
        "worker_restarted": bool(record.get("worker_restarted", False)),
    }


def render_case(case: dict[str, Any]) -> str:
    """The volatile half: one scenario's evidence, as an on-call engineer sees it."""
    lines = ["# Incident evidence", "", "## HTTP deliveries observed at ingest"]
    if not case["deliveries"]:
        lines.append(
            "(none — no HTTP request reached ingest for this event at all)"
        )
    else:
        for i, d in enumerate(case["deliveries"], 1):
            status = d["http_status"] if d["http_status"] is not None else "no response"
            body = (d["response_body"] or "").strip()
            detail = f" response={body}" if body else ""
            err = f" error={d['error']}" if d.get("error") else ""
            arrived = d.get("arrived_after_ms")
            when = f"arrived +{arrived} ms" if arrived is not None else "arrival unknown"
            lines.append(
                f"{i}. [{when}] HTTP {status} after {d['duration_ms']} ms{detail}{err}"
            )
            upstream = d.get("upstream_status")
            if upstream is not None and upstream != d["http_status"]:
                lines.append(
                    f"   the sender was answered HTTP {upstream} for this attempt"
                )
            if d.get("signature_header"):
                parses = d.get("body_parses_as_json")
                lines.append(
                    f"   request: Stripe-Signature: {d['signature_header']}"
                )
                lines.append(
                    f"   request body: {d.get('body_bytes')} bytes, "
                    f"valid JSON: {'yes' if parses else 'no'}"
                )

    lines += ["", "## Event log rows recorded by ingest"]
    if not case["event_log"]:
        lines.append("(none — ingest recorded no row for this event)")
    else:
        for row in case["event_log"]:
            lines.append(
                f"- id={row.get('id')} type={row.get('event_type')} "
                f"object={row.get('object_id')} status={row.get('status')}"
            )

    lines += ["", "## Ledger balance change over the incident (minor units)"]
    if not case["ledger_diff"]:
        lines.append("(no balances changed)")
    else:
        for account, delta in sorted(case["ledger_diff"].items()):
            lines.append(f"- {account}: {delta:+d}")

    lines += [
        "",
        "## Infrastructure during this window",
        f"- worker process restarted: {'yes' if case.get('worker_restarted') else 'no'}",
        "",
        f"## Money conservation: {'holds' if case['invariant_holds'] else 'BROKEN'}",
        "",
        "Diagnose this incident.",
    ]
    return "\n".join(lines)


def build_messages(case: dict[str, Any]) -> list[dict[str, Any]]:
    """The user turn — always AFTER the cached prefix."""
    return [{"role": "user", "content": render_case(case)}]

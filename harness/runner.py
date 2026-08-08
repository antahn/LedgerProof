"""Scenario runner (Phase 2).

Contract: for every scenario — snapshot invariant, inject, wait for
quiescence, re-check invariant, and emit one JSONL record to artifacts/:

  {"scenario_id": ..., "fault": ..., "params": {...}, "events": [...],
   "invariant_before": true, "invariant_after": ..., "ledger_diff": {...},
   "duration_ms": ...}

Artifacts are raw run output, never hand-edited; every number quoted anywhere
in this repo traces back to one of these records.

Break detection is `detect_breaks(plan, observation)` — a pure function of what
the plan SAID should happen and what the ledger ACTUALLY did. It cannot see the
scenario label by construction (it is never passed one), which is what keeps
the Phase 5 benchmark's ground truth honest: the label and the verdict derive
from the same plan, but the verdict never reads the label.

Two things the runner records rather than repairs:
- A scenario that leaves the invariant broken triggers `reset_ledger()` before
  the next one, so one break cannot contaminate every record after it. The
  reset is recorded in the record that caused it.
- Everything else (a lost event, a stuck queue, a double post) is written down
  exactly as observed. Fixing product bugs is a separate job from finding them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from harness.faults import Fault, FaultPlan, plan
from harness.proxy import ChaosProxy, DeliveryResult
from harness.scenarios import Scenario, ScenarioSet, Skip, generate
from harness.stack import REPO_ROOT, Stack
from ledgerproof.ledger import invariant
from ledgerproof.ledger.balances import all_balances
from ledgerproof.recon.breaks import BreakKind
from ledgerproof.recon.reconciler import reconcile_expected

ARTIFACTS = REPO_ROOT / "artifacts"

# A scenario whose work never finishes is a finding, not a reason to hang: the
# PARTIAL_WRITE fault is expected to spend this whole budget, so it is short.
QUIESCENT_TIMEOUT_S = 15.0
PROXY_TIMEOUT_S = 30.0
HEALTH_PROBE_INTERVAL_S = 0.4

# Break kinds this runner emits. Seven come from the brief's Phase 2 contract;
# INGEST_UNRESPONSIVE is measured only for SLOW_LORIS, where "ingest doesn't
# wedge" is the whole point and the ledger cannot show it.
INVARIANT_VIOLATION = "INVARIANT_VIOLATION"
LEDGER_DIFF_MISMATCH = "LEDGER_DIFF_MISMATCH"
ACCEPTED_BAD_DELIVERY = "ACCEPTED_BAD_DELIVERY"
LOST_EVENT = "LOST_EVENT"
DOUBLE_POST = "DOUBLE_POST"
NOT_QUIESCENT = "NOT_QUIESCENT"
RECONCILER_MISSED_DROP = "RECONCILER_MISSED_DROP"
INGEST_UNRESPONSIVE = "INGEST_UNRESPONSIVE"
# Refused by the wrong layer. Added after a mutation study showed TRUNCATE_BODY
# reading green with HMAC verification entirely deleted: a truncated body is
# unparseable JSON, so the 400 came from the parser and the fault proved
# nothing about the signature.
WRONG_REJECTION_REASON = "WRONG_REJECTION_REASON"


@dataclass(frozen=True)
class Observation:
    """What the system actually did, with no reference to the injected fault."""

    ledger_diff: dict[str, int]
    invariant_after: bool
    posted: tuple[str, ...]
    transactions: int
    quiescent: bool
    accepted_rejected: tuple[str, ...] = ()
    wrong_rejection_reason: tuple[str, ...] = ()
    recon_reported_break: bool = False
    ingest_responsive: bool | None = None  # None: not measured for this fault


def _break(kind: str, detail: str) -> dict[str, str]:
    return {"kind": kind, "detail": detail}


def detect_breaks(fault_plan: FaultPlan, obs: Observation) -> list[dict[str, str]]:
    """Diff what the plan promised against what the ledger did.

    Takes a plan and an observation — never a Scenario — so the label cannot
    leak into a verdict even by accident. Most severe first.
    """
    exp = fault_plan.expectation
    breaks: list[dict[str, str]] = []

    if not obs.invariant_after:
        breaks.append(
            _break(
                INVARIANT_VIOLATION,
                "money conservation broke: the ledger created or destroyed money",
            )
        )

    if obs.ledger_diff != exp.balances_delta:
        breaks.append(
            _break(
                LEDGER_DIFF_MISMATCH,
                f"expected {_fmt(exp.balances_delta)}, observed {_fmt(obs.ledger_diff)}",
            )
        )

    bad: dict[str, list[str]] = {}
    for event_id in exp.rejected_event_ids & set(obs.posted):
        bad.setdefault(event_id, []).append("it produced a ledger transaction")
    for event_id in obs.accepted_rejected:
        bad.setdefault(event_id, []).append("ingest answered 2xx")
    for event_id in sorted(bad):
        breaks.append(
            _break(
                ACCEPTED_BAD_DELIVERY,
                f"{event_id} should have been refused but " + " and ".join(bad[event_id]),
            )
        )

    for detail in obs.wrong_rejection_reason:
        breaks.append(_break(WRONG_REJECTION_REASON, detail))

    for event_id in sorted(exp.posted_event_ids - set(obs.posted)):
        breaks.append(
            _break(LOST_EVENT, f"{event_id} should have posted exactly one transaction, found none")
        )

    if obs.transactions > len(exp.posted_event_ids):
        breaks.append(
            _break(
                DOUBLE_POST,
                f"{obs.transactions} transactions for {len(exp.posted_event_ids)} "
                f"expected posting(s): {list(obs.posted)}",
            )
        )

    if not obs.quiescent:
        breaks.append(
            _break(
                NOT_QUIESCENT,
                "the queue or the event log never drained: work is stuck in flight",
            )
        )

    if exp.reconciler_should_report_break and not obs.recon_reported_break:
        breaks.append(
            _break(
                RECONCILER_MISSED_DROP,
                "the ledger looks healthy and the reconciler reported no break for the "
                "undelivered event — the one failure only reconciliation can catch",
            )
        )

    if obs.ingest_responsive is False:
        breaks.append(
            _break(
                INGEST_UNRESPONSIVE,
                "ingest stopped answering /healthz while a body was still on the wire",
            )
        )

    return breaks


def _fmt(delta: dict[str, int]) -> str:
    return "{" + ", ".join(f"{k}={v:+d}" for k, v in sorted(delta.items())) + "}"


def _diff(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """Non-zero balance changes. Zero-net accounts are omitted on both sides."""
    accounts = set(before) | set(after)
    changes = {name: after.get(name, 0) - before.get(name, 0) for name in accounts}
    return {name: value for name, value in sorted(changes.items()) if value != 0}


_TASK_RECEIVED = re.compile(r"process_event\[([0-9a-f-]+)\] received")
_TASK_SUCCEEDED = re.compile(r"process_event\[([0-9a-f-]+)\] succeeded")


def task_in_flight(worker_log: str) -> bool:
    """Did a task start in this slice of worker log and never finish?

    PARTIAL_WRITE claims to SIGKILL the worker *mid-transaction*. Whether the
    kill actually landed inside a ledger write is a property of process timing
    on the host, not something the fault can assert, so every kill scenario
    measures it and records the answer. A run that never caught a task in
    flight proves crash-then-redeliver safety, NOT write atomicity, and the
    artifact has to say which one it earned.

    Errs toward claiming coverage: a task killed in the microseconds between
    COMMIT and its own "succeeded" log line reads as in-flight.
    """
    finished = set(_TASK_SUCCEEDED.findall(worker_log))
    return any(task not in finished for task in _TASK_RECEIVED.findall(worker_log))


def _log_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _log_since(path: Path, offset: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read().decode("utf-8", errors="replace")


def _posted_rows(db_url: str, event_ids: Sequence[str]) -> list[str]:
    """The stripe_event_id of every transaction one of these events produced."""
    with psycopg.connect(db_url) as conn:
        rows = conn.execute(
            "SELECT stripe_event_id FROM transactions WHERE stripe_event_id = ANY(%s)",
            (list(event_ids),),
        ).fetchall()
    return [row[0] for row in rows]


def _delivery_records(
    fault_plan: FaultPlan,
    results: Sequence[DeliveryResult],
    *,
    scenario_started: float,
    scenario_started_wall: float,
) -> list[dict[str, object]]:
    """What was sent and what came back, per delivery.

    The REQUEST side is recorded, not just the response, because four faults
    (TAMPER_BODY, TRUNCATE_BODY, STALE_TIMESTAMP, DOWNGRADE_SCHEME) are
    identical from the response alone — every one is a 400 'invalid signature'
    with no ledger movement. An engineer triaging a rejected webhook has the
    request in front of them; withholding it would make those four classes
    indistinguishable by construction and cap any classifier at chance across
    them. The signature header, the body length, and whether the body still
    parses as JSON separate all four.

    Only the header and metadata are kept — never the signing secret, which
    appears nowhere in a delivery.
    """
    records: list[dict[str, object]] = []
    planned = list(fault_plan.deliveries)
    for index, result in enumerate(results):
        # Retries and redeliveries reuse the first delivery's bytes.
        sent = planned[index] if index < len(planned) else (planned[0] if planned else None)
        record: dict[str, object] = {
            "event_id": result.event_id,
            "status_code": result.status_code,
            # The response body says WHICH layer refused: the HMAC check, the
            # JSON parser, the envelope check, or the size cap.
            "body": result.body,
            "error": result.error,
            "duration_ms": result.duration_ms,
            # When this delivery hit the wire, relative to the event being
            # generated. Two faults are invisible without it: DELAY (a long
            # gap before a single otherwise-normal delivery) and
            # CONCURRENT_DUPLICATE (deliveries that OVERLAP rather than
            # stagger — the taxonomy names timing as the giveaway, and until
            # now the evidence did not carry any).
            "arrived_after_ms": max(0, int((result.started_at - scenario_started) * 1000)),
            # What the SENDER saw. RESPOND_500 forces the proxy to answer 500
            # upstream while still forwarding, so ingest records an ordinary
            # 200 and the defining event appears nowhere unless recorded here.
            # Stripe's own delivery log is exactly this field.
            "upstream_status": (
                500 if (fault_plan.force_500 and index == 0) else result.status_code
            ),
        }
        if sent is not None:
            body = sent.body
            record["request_signature_header"] = sent.sig_header
            record["request_body_bytes"] = len(body)
            record["request_body_parses_as_json"] = _parses_as_json(body)
            # How old the signature was when it landed. Without this the
            # header's `t=` is an unanchored number: nothing else in the
            # evidence says what "now" was, so a back-dated signature is
            # indistinguishable from a tampered one. This is precisely the
            # quantity ingest's 300-second tolerance check computes.
            arrived_wall = scenario_started_wall + (result.started_at - scenario_started)
            signed_at = _signature_timestamp(sent.sig_header)
            if signed_at is not None:
                record["signature_age_at_arrival_s"] = round(arrived_wall - signed_at, 1)
        records.append(record)
    return records


def _signature_timestamp(sig_header: str) -> float | None:
    """The `t=` value from a Stripe-Signature header, if it carries one."""
    for item in sig_header.split(","):
        key, sep, value = item.partition("=")
        if sep and key.strip() == "t":
            try:
                return float(value)
            except ValueError:
                return None
    return None


def _parses_as_json(body: bytes) -> bool:
    try:
        json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return True


def _event_log_rows(db_url: str, event_ids: Sequence[str]) -> list[dict[str, str | None]]:
    """The stripe_events rows for these events, as ingest recorded them.

    This is the evidence the Phase 5 agent reasons over, so it is captured per
    scenario rather than reconstructed later: the ledger is shared across a run
    and a row's status keeps moving after the scenario ends.
    """
    with psycopg.connect(db_url) as conn:
        rows = conn.execute(
            "SELECT id, event_type, object_id, status FROM stripe_events"
            " WHERE id = ANY(%s) ORDER BY received_at",
            (list(event_ids),),
        ).fetchall()
    return [
        {"id": r[0], "event_type": r[1], "object_id": r[2], "status": r[3]} for r in rows
    ]


class _HealthProbe(threading.Thread):
    """Polls /healthz while a slow body is on the wire.

    SLOW_LORIS is the one fault whose failure mode is invisible in the ledger:
    a wedged ingest posts nothing and looks exactly like a clean rejection.
    """

    def __init__(self, stack: Stack, interval: float = HEALTH_PROBE_INTERVAL_S) -> None:
        super().__init__(daemon=True)
        self.stack = stack
        self.interval = interval
        self.samples: list[bool] = []
        # NOT `_stop`: threading.Thread._stop is a real method CPython calls
        # from join(), and shadowing it with an Event breaks every join.
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.wait(self.interval):
            self.samples.append(self.stack.ingest_healthy(timeout=2.0))

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=10.0)

    @property
    def responsive(self) -> bool | None:
        return all(self.samples) if self.samples else None


def run_scenario(
    stack: Stack,
    scenario: Scenario,
    *,
    quiescent_timeout: float = QUIESCENT_TIMEOUT_S,
    proxy_timeout: float = PROXY_TIMEOUT_S,
) -> dict:
    """Inject one scenario and return its JSONL record.

    Re-plans the scenario with the stack's own webhook secret (the harness signs
    its own deliveries; nothing here depends on live Stripe), drives it through
    the proxy, waits for the system to go quiet, and diffs the result against
    the plan's Expectation.
    """
    started_at = datetime.now(UTC)
    t0 = time.perf_counter()
    # Same clock the proxy stamps deliveries with, so offsets are comparable.
    scenario_started = time.monotonic()
    # Captured at the same instant, so a monotonic delivery offset converts
    # exactly to wall clock — which is what a signature timestamp compares to.
    scenario_started_wall = time.time()

    fault_plan = plan(
        scenario.fault, scenario.fixtures, stack.webhook_secret, params=scenario.params
    )
    exp = fault_plan.expectation

    before = all_balances(stack.db_url)
    invariant_before = invariant.check(stack.db_url).ok

    proxy = ChaosProxy(stack.ingest_url, timeout=proxy_timeout)
    kill_errors: list[str] = []

    def on_kill() -> None:
        try:
            stack.kill_worker()
        except Exception as exc:  # noqa: BLE001 — a failed kill is a recorded fact
            kill_errors.append(f"{type(exc).__name__}: {exc}")

    probe = _HealthProbe(stack) if any(d.slow_loris_s > 0 for d in fault_plan.deliveries) else None
    if probe is not None:
        probe.start()
    kills = fault_plan.kill_worker_after_s is not None
    log_offset = _log_size(stack.worker_log_path) if kills else 0
    try:
        results: list[DeliveryResult] = proxy.execute(
            fault_plan, on_kill=on_kill if kills else None
        )
    finally:
        if probe is not None:
            probe.stop()

    # Read before the restart appends to the same file.
    killed_in_flight = (
        task_in_flight(_log_since(stack.worker_log_path, log_offset)) if kills else None
    )

    planned = len(fault_plan.deliveries)
    worker_restarted = False
    if kills:
        # execute() returns only after the kill timer has fired, so the worker
        # is already down and the restart cannot race it.
        stack.restart_worker()
        worker_restarted = True
        if fault_plan.redeliver_after_kill and fault_plan.deliveries:
            results.append(proxy.send(fault_plan.deliveries[0]))

    # Scoped to this scenario's events: a stuck row from an earlier scenario is
    # that scenario's finding, not this one's.
    quiescent = stack.wait_quiescent(
        timeout=quiescent_timeout, event_ids=[fx.event_id for fx in scenario.fixtures]
    )

    after = all_balances(stack.db_url)
    invariant_after = invariant.check(stack.db_url)
    ledger_diff = _diff(before, after)

    # Deliveries carry ids the fixtures do not: DUPLICATE_OBJECT's twin
    # envelope is a second event id for the same money movement.
    candidates = tuple(
        dict.fromkeys(
            [fx.event_id for fx in scenario.fixtures] + [d.event_id for d in fault_plan.deliveries]
        )
    )
    rows = _posted_rows(stack.db_url, candidates)
    posted = tuple(sorted(set(rows)))
    event_log = _event_log_rows(stack.db_url, candidates)

    accepted_rejected = tuple(
        dict.fromkeys(
            delivery.event_id
            for delivery, result in zip(fault_plan.deliveries, results[:planned], strict=False)
            if delivery.expect_rejected
            and result.status_code is not None
            and 200 <= result.status_code < 300
        )
    )

    # Refused, but by the right layer? A signature attack that trips the JSON
    # parser instead of the HMAC check is a green result proving nothing.
    wrong_reason = tuple(
        dict.fromkeys(
            f"{delivery.event_id}: expected refusal by {delivery.expect_rejection_reason!r}, "
            f"got HTTP {result.status_code} {result.body[:120]!r}"
            for delivery, result in zip(fault_plan.deliveries, results[:planned], strict=False)
            if delivery.expect_rejection_reason is not None
            and result.status_code is not None
            and 400 <= result.status_code < 500
            and delivery.expect_rejection_reason not in result.body
        )
    )

    recon_record: dict | None = None
    recon_reported = False
    if exp.reconciler_should_report_break:
        # DROP leaves a balanced, healthy-looking ledger; only an external
        # comparison can see the hole, so the reconciler is under test too.
        recon = reconcile_expected(stack.db_url, scenario.fixtures)
        missing = [
            b
            for b in recon.breaks
            if b.kind is BreakKind.MISSING_IN_LEDGER and b.event_id in set(candidates)
        ]
        recon_reported = bool(missing)
        recon_record = {
            "reported": recon_reported,
            "checked": recon.checked,
            "invariant_ok": recon.invariant_ok,
            "breaks": [b.as_dict() for b in missing],
        }

    obs = Observation(
        ledger_diff=ledger_diff,
        invariant_after=invariant_after.ok,
        posted=posted,
        transactions=len(rows),
        quiescent=quiescent,
        accepted_rejected=accepted_rejected,
        wrong_rejection_reason=wrong_reason,
        recon_reported_break=recon_reported,
        ingest_responsive=probe.responsive if probe is not None else None,
    )
    breaks = detect_breaks(fault_plan, obs)

    ledger_reset = False
    if not invariant_after.ok:
        # Contain the damage: an unbalanced ledger would make every later
        # scenario's invariant_after False for a break it did not cause.
        stack.reset_ledger()
        ledger_reset = True

    return {
        "scenario_id": scenario.scenario_id,
        "fault": scenario.fault.value,
        "params": scenario.params,
        "events": [fx.event_id for fx in scenario.fixtures],
        "invariant_before": invariant_before,
        "invariant_after": invariant_after.ok,
        "ledger_diff": ledger_diff,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "deliveries": _delivery_records(
            fault_plan,
            results,
            scenario_started=scenario_started,
            scenario_started_wall=scenario_started_wall,
        ),
        # What ingest durably recorded, as an on-call engineer would query it.
        "event_log": event_log,
        "expected": {
            "posted": sorted(exp.posted_event_ids),
            "rejected": sorted(exp.rejected_event_ids),
            "balances_delta": dict(exp.balances_delta),
        },
        "actual": {"posted": list(posted), "transactions": len(rows)},
        "breaks": breaks,
        "quiescent": quiescent,
        "label": scenario.label,
        # --- extras, beyond the pinned shape ---------------------------------
        "kinds": [fx.kind for fx in scenario.fixtures],
        "started_at": started_at.isoformat(),
        "accepted_rejected": list(accepted_rejected),
        "wrong_rejection_reason": list(wrong_reason),
        "redelivered_after_kill": worker_restarted and fault_plan.redeliver_after_kill,
        "worker_restarted": worker_restarted,
        "killed_with_task_in_flight": killed_in_flight,
        "ledger_reset": ledger_reset,
        "ingest_responsive": obs.ingest_responsive,
        "recon": recon_record,
        "invariant_detail": invariant_after.as_dict(),
        "error": "; ".join(kill_errors) or None,
    }


def observation_from_record(record: dict) -> Observation:
    """Rebuild the Observation a record was judged from.

    Every input to `detect_breaks` is in the artifact, so any break in any
    record can be re-derived — and audited — from the file alone.
    """
    return Observation(
        ledger_diff={k: int(v) for k, v in record["ledger_diff"].items()},
        invariant_after=bool(record["invariant_after"]),
        posted=tuple(record["actual"]["posted"]),
        transactions=int(record["actual"]["transactions"]),
        quiescent=bool(record["quiescent"]),
        accepted_rejected=tuple(record.get("accepted_rejected", ())),
        wrong_rejection_reason=tuple(record.get("wrong_rejection_reason", ())),
        recon_reported_break=bool((record.get("recon") or {}).get("reported", False)),
        ingest_responsive=record.get("ingest_responsive"),
    )


def _failure_record(scenario: Scenario, exc: BaseException, duration_ms: int) -> dict:
    """A record for a scenario that blew up, in the same shape as any other."""
    return {
        "scenario_id": scenario.scenario_id,
        "fault": scenario.fault.value,
        "params": scenario.params,
        "events": [fx.event_id for fx in scenario.fixtures],
        "invariant_before": None,
        "invariant_after": None,
        "ledger_diff": {},
        "duration_ms": duration_ms,
        "deliveries": [],
        "expected": {"posted": [], "rejected": [], "balances_delta": {}},
        "actual": {"posted": [], "transactions": 0},
        "breaks": [],
        "quiescent": False,
        "label": scenario.label,
        "kinds": [fx.kind for fx in scenario.fixtures],
        "error": f"{type(exc).__name__}: {exc}",
    }


@contextmanager
def _jsonl(path: Path) -> Iterator[Callable[[dict], None]]:
    """One JSON object per line, flushed as it goes: a crash keeps what ran."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:

        def emit(record: dict) -> None:
            handle.write(json.dumps(record, default=str) + "\n")
            handle.flush()

        yield emit


def run_all(
    stack: Stack,
    scenarios: Sequence[Scenario],
    out_path: str | Path,
    *,
    reset_each: bool = False,
    quiescent_timeout: float = QUIESCENT_TIMEOUT_S,
    echo: bool = False,
) -> list[dict]:
    """Run every scenario against one live stack, writing JSONL as it goes."""
    records: list[dict] = []
    with _jsonl(Path(out_path)) as emit:
        for scenario in scenarios:
            if reset_each:
                stack.reset_ledger()
            started = time.perf_counter()
            try:
                record = run_scenario(stack, scenario, quiescent_timeout=quiescent_timeout)
            except Exception as exc:  # noqa: BLE001 — a crashed scenario is a record, not a lost run
                record = _failure_record(scenario, exc, int((time.perf_counter() - started) * 1000))
            emit(record)
            records.append(record)
            if echo:
                print(
                    f"  {record['scenario_id']:<44} {record['duration_ms']:>6}ms "
                    f"breaks={[b['kind'] for b in record['breaks']]}"
                    + (f" error={record['error']}" if record.get("error") else ""),
                    flush=True,
                )
    return records


def summarize(
    records: Sequence[dict],
    *,
    argv: Sequence[str],
    skipped: Sequence[Skip],
    generated: int,
    wall_clock_s: float,
    started_at: str,
    artifact: str,
) -> dict:
    """Counts by fault, breaks by kind, and everything that was NOT run."""
    counts_by_fault: dict[str, int] = {}
    breaks_by_kind: dict[str, int] = {}
    for record in records:
        counts_by_fault[record["fault"]] = counts_by_fault.get(record["fault"], 0) + 1
        for item in record["breaks"]:
            breaks_by_kind[item["kind"]] = breaks_by_kind.get(item["kind"], 0) + 1
    kills = [r for r in records if r.get("killed_with_task_in_flight") is not None]
    return {
        "argv": list(argv),
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "wall_clock_s": round(wall_clock_s, 2),
        "artifact": artifact,
        "scenarios_generated": generated,
        "scenarios_run": len(records),
        "scenarios_with_breaks": sum(1 for r in records if r["breaks"]),
        "scenarios_errored": sum(1 for r in records if r.get("error")),
        "counts_by_fault": dict(sorted(counts_by_fault.items())),
        "breaks_by_kind": dict(sorted(breaks_by_kind.items())),
        # Coverage, not results: how many worker kills actually landed while a
        # task was running. Zero means the run proved crash-then-redeliver
        # safety and NOT write atomicity.
        "kill_scenarios": len(kills),
        "kills_with_task_in_flight": sum(1 for r in kills if r["killed_with_task_in_flight"]),
        "skipped_combos": [s.as_dict() for s in skipped],
    }


def _fault_list(raw: str | None) -> Sequence[Fault] | None:
    if not raw:
        return None
    return [Fault(name.strip().upper()) for name in raw.split(",") if name.strip()]


def _kind_list(raw: str | None) -> Sequence[str] | None:
    if not raw:
        return None
    return [name.strip() for name in raw.split(",") if name.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the LedgerProof chaos harness.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--per-combo", type=int, default=1)
    parser.add_argument("--faults", default=None, help="comma-separated, e.g. NONE,DUPLICATE")
    parser.add_argument("--kinds", default=None, help="comma-separated event kinds")
    parser.add_argument(
        "--out", default=None, help="JSONL path (default artifacts/chaos_<utc-stamp>.jsonl)"
    )
    parser.add_argument("--limit", type=int, default=None, help="run only the first N scenarios")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument(
        "--keep-stack",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="reuse one stack for every scenario (default: on)",
    )
    parser.add_argument(
        "--reset-each", action="store_true", help="empty the ledger before each scenario"
    )
    parser.add_argument("--quiescent-timeout", type=float, default=QUIESCENT_TIMEOUT_S)
    args = parser.parse_args()

    started = datetime.now(UTC)
    t0 = time.perf_counter()

    scenarios: ScenarioSet = generate(
        seed=args.seed,
        faults=_fault_list(args.faults),
        kinds=_kind_list(args.kinds),
        per_combo=args.per_combo,
    )
    generated = len(scenarios)
    skipped = scenarios.skipped
    selected: Sequence[Scenario] = scenarios[: args.limit] if args.limit else scenarios

    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else ARTIFACTS / f"chaos_{stamp}.jsonl"
    summary_path = out.with_name(f"{out.stem}_summary.json")

    print(
        f"chaos: {len(selected)}/{generated} scenarios, {len(skipped)} combos skipped -> {out}",
        flush=True,
    )

    stack = Stack(port=args.port)
    stack.create_database()
    if args.keep_stack:
        with stack:
            records = run_all(
                stack,
                selected,
                out,
                reset_each=args.reset_each,
                quiescent_timeout=args.quiescent_timeout,
                echo=True,
            )
    else:
        records = []
        with _jsonl(out) as emit:
            for scenario in selected:
                with Stack(port=args.port) as fresh:
                    if args.reset_each:
                        fresh.reset_ledger()
                    record = run_scenario(fresh, scenario, quiescent_timeout=args.quiescent_timeout)
                emit(record)
                records.append(record)

    summary = summarize(
        records,
        argv=sys.argv,
        skipped=skipped,
        generated=generated,
        wall_clock_s=time.perf_counter() - t0,
        started_at=started.isoformat(),
        artifact=str(out),
    )
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()

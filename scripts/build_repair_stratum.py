"""Generate the repair stratum by running the harness against broken builds.

The Phase 2 sweep produces no repairable damage, because the system is correct:
315 scenarios, zero breaks. A repair metric needs ledgers that are actually
wrong, so this reproduces two of the mutation study's deliberate defects and
captures what the harness sees.

    M5  dedupe removed at every layer  -> the same payment posts N times.
        The books stay internally consistent (both sides doubled), so the
        invariant is silent, but the balances disagree with the events.
        REPAIRABLE: reverse the surplus copies.

    M7  balance trigger dropped, charge debits the full gross
        -> a transaction lands that debits more than it credits.
        NOT REPAIRABLE by a compensating transaction: a balanced transaction
        provably cannot move the invariant gap (tests/unit/test_repair_algebra),
        so nothing the database would accept can close it. Kept as a negative
        control — an agent that confidently proposes a fix here is wrong.

Mutations are applied to a clean tree and reverted with `git checkout --` in a
finally block, so an interrupted run cannot leave a broken build behind.

    uv run python scripts/build_repair_stratum.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO / "artifacts" / "mutation"
OUT = REPO / "artifacts" / "repair_stratum.jsonl"


@dataclass(frozen=True)
class Edit:
    path: str
    old: str
    new: str


@dataclass(frozen=True)
class Mutation:
    key: str
    description: str
    edits: tuple[Edit, ...]
    faults: str
    kinds: str
    per_combo: int
    repairable: bool
    damage: str


MUTATIONS = (
    Mutation(
        key="m5_double_post",
        description=(
            "dedupe removed at every layer: ingest fast path reports every event as "
            "new, both transactions UNIQUE constraints dropped, and the deterministic "
            "uuid5 transaction id replaced with a random uuid4"
        ),
        edits=(
            Edit(
                "src/ledgerproof/ingest/dedupe.py",
                "        object_id = money_movement_object_id(event)",
                "        return DedupeResult(new=True)  # MUTATION M5\n"
                "        object_id = money_movement_object_id(event)",
            ),
            # Absorbs the comma after `memo`: dropping only the two CONSTRAINT
            # lines leaves a trailing comma before `)` and the migration fails
            # to parse, which reads as a broken harness rather than a mutation.
            Edit(
                "migrations/001_ledger.sql",
                "  memo          TEXT,\n"
                "  -- Stripe's own documented dedupe key: event.id is NOT sufficient, because\n"
                "  -- two distinct Event objects can be generated for the same state change.\n"
                "  CONSTRAINT dedupe_event  UNIQUE (stripe_event_id),\n"
                "  CONSTRAINT dedupe_object UNIQUE (event_type, stripe_object_id)",
                "  memo          TEXT\n"
                "  -- MUTATION M5: both dedupe constraints removed",
            ),
            Edit(
                "src/ledgerproof/stripe_io/mapping.py",
                'id=uuid.uuid5(uuid.NAMESPACE_URL, "ledgerproof:" + event["id"]),',
                "id=uuid.uuid4(),  # MUTATION M5",
            ),
        ),
        faults="DUPLICATE,CONCURRENT_DUPLICATE,RESPOND_500",
        kinds="charge_succeeded,charge_refunded,payout_paid,dispute_created",
        per_combo=3,
        repairable=True,
        damage="duplicate_post",
    ),
    Mutation(
        key="m7_unbalanced",
        description=(
            "the deferred balance trigger is dropped and charge.succeeded debits the "
            "full gross instead of gross minus fee, so an unbalanced transaction lands"
        ),
        edits=(
            Edit(
                "migrations/001_ledger.sql",
                "CREATE CONSTRAINT TRIGGER entries_balanced\n"
                "  AFTER INSERT ON entries\n"
                "  DEFERRABLE INITIALLY DEFERRED\n"
                "  FOR EACH ROW EXECUTE FUNCTION assert_txn_balanced();",
                "-- MUTATION M7: balance trigger removed",
            ),
            Edit(
                "src/ledgerproof/stripe_io/mapping.py",
                'add("stripe_balance", "debit", gross - fee)',
                'add("stripe_balance", "debit", gross)  # MUTATION M7',
            ),
        ),
        faults="NONE",
        kinds="charge_succeeded",
        per_combo=8,
        repairable=False,
        damage="unbalanced_write",
    ),
)


def run(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=REPO, capture_output=True, text=True, check=check)


def tree_is_clean() -> bool:
    """No modifications to TRACKED files.

    Untracked files are ignored deliberately: this script's own output lands in
    artifacts/, and counting it as dirt would make the post-revert assertion
    fail on a successful run.
    """
    return not run(
        "git", "status", "--porcelain", "--untracked-files=no"
    ).stdout.strip()


def apply(mutation: Mutation) -> None:
    for edit in mutation.edits:
        path = REPO / edit.path
        text = path.read_text(encoding="utf-8")
        if edit.old not in text:
            raise SystemExit(f"{mutation.key}: anchor not found in {edit.path}")
        path.write_text(text.replace(edit.old, edit.new, 1), encoding="utf-8")


def revert(mutation: Mutation) -> None:
    run("git", "checkout", "--", *{e.path for e in mutation.edits})


def sweep(mutation: Mutation) -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = ARTIFACTS / f"{mutation.key}_stratum.jsonl"
    result = run(
        "uv", "run", "python", "-m", "harness.runner",
        "--seed", "23",
        "--per-combo", str(mutation.per_combo),
        "--faults", mutation.faults,
        "--kinds", mutation.kinds,
        "--out", str(out),
        "--port", "8100",
        check=False,
    )
    if not out.exists():
        sys.stderr.write(result.stdout[-3000:] + result.stderr[-3000:])
        raise SystemExit(f"{mutation.key}: harness produced no artifact")
    return out


def main() -> None:
    if not tree_is_clean():
        raise SystemExit("working tree is dirty; commit or stash before mutating it")

    records: list[dict] = []
    for mutation in MUTATIONS:
        print(f"=== {mutation.key}: {mutation.description}")
        try:
            apply(mutation)
            artifact = sweep(mutation)
        finally:
            revert(mutation)
            assert tree_is_clean(), "failed to restore the tree after mutating it"

        for line in artifact.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("error"):
                continue
            # The ground truth a repair is scored against: what the delivered
            # events SHOULD have produced, which the harness knows exactly.
            record["repair_label"] = {
                "mutation": mutation.key,
                "damage": mutation.damage,
                "repairable_by_compensating_transaction": mutation.repairable,
                "expected_balances_delta": dict(
                    (record.get("expected") or {}).get("balances_delta") or {}
                ),
                "observed_balances_delta": dict(record.get("ledger_diff") or {}),
            }
            records.append(record)

        damaged = sum(
            1
            for r in records
            if r["repair_label"]["mutation"] == mutation.key
            and r["repair_label"]["observed_balances_delta"]
            != r["repair_label"]["expected_balances_delta"]
        )
        print(f"    {mutation.key}: {damaged} of {mutation.per_combo * 100 // 100} "
              f"scenario groups show damaged books")

    OUT.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    by_mutation: dict[str, int] = {}
    damaged_total = 0
    for r in records:
        label = r["repair_label"]
        by_mutation[label["mutation"]] = by_mutation.get(label["mutation"], 0) + 1
        if label["observed_balances_delta"] != label["expected_balances_delta"]:
            damaged_total += 1
    print(json.dumps(
        {"artifact": str(OUT), "records": len(records),
         "by_mutation": by_mutation, "with_damaged_books": damaged_total},
        indent=2,
    ))


if __name__ == "__main__":
    main()

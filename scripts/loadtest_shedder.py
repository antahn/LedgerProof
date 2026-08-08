"""Load-test the shedder and record the pressure at which each tier engages.

What matters is the ACTUAL numbers, not the configured thresholds — the
threshold is the intent, the engagement point is the behaviour, and the
anti-flap machinery deliberately puts them in different places: a tier does not
shed the instant pressure crosses its threshold, but only after enough
consecutive samples agree.

Runs the real LoadShedder against a synthetic offered-load ramp (no network, so
the numbers are about the shedding policy rather than this laptop's sockets),
and writes artifacts/shed_loadtest.json.

    uv run python scripts/loadtest_shedder.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ledgerproof.limits.shed import DEFAULT_THRESHOLDS, LoadShedder, Tier

ARTIFACT = Path(__file__).resolve().parent.parent / "artifacts" / "shed_loadtest.json"

RAMP_STEPS = 200          # samples from idle to overload
REQUESTS_PER_STEP = 50    # offered requests at each pressure level
TIER_MIX = {              # a plausible production mix
    Tier.TEST_MODE: 10,
    Tier.GET: 20,
    Tier.POST: 15,
    Tier.CRITICAL: 5,
}


def ramp(shedder: LoadShedder) -> dict:
    """Ramp pressure 0 -> 1 and record where each tier first sheds."""
    engaged_at: dict[str, float] = {}
    served: dict[str, int] = {t.name: 0 for t in Tier}
    dropped: dict[str, int] = {t.name: 0 for t in Tier}

    for step in range(RAMP_STEPS + 1):
        pressure = step / RAMP_STEPS
        shedder.observe(pressure)
        for tier, weight in TIER_MIX.items():
            for _ in range(weight * REQUESTS_PER_STEP // 50):
                decision = shedder.should_shed(tier)
                if decision.shed:
                    dropped[tier.name] += 1
                    engaged_at.setdefault(tier.name, round(pressure, 4))
                else:
                    served[tier.name] += 1
    return {"engaged_at": engaged_at, "served": served, "dropped": dropped}


def recovery(shedder: LoadShedder) -> dict:
    """From full overload, ramp back down and record where each tier stops."""
    released_at: dict[str, float] = {}
    for step in range(RAMP_STEPS + 1):
        pressure = 1.0 - step / RAMP_STEPS
        shedder.observe(pressure)
        for tier in Tier:
            if not shedder.should_shed(tier).shed:
                released_at.setdefault(tier.name, round(pressure, 4))
    return released_at


def throughput(shedder: LoadShedder, samples: int = 200_000) -> float:
    """Decisions per second: the shedder must be far cheaper than the work."""
    shedder.observe(0.8)
    start = time.perf_counter()
    for _ in range(samples):
        shedder.should_shed(Tier.POST)
    return round(samples / (time.perf_counter() - start))


def main() -> None:
    live = LoadShedder()
    ramp_result = ramp(live)
    released_at = recovery(live)

    dark = LoadShedder(enforce=False)
    dark_result = ramp(dark)

    payload = {
        "configured_thresholds": {t.name: DEFAULT_THRESHOLDS[t] for t in Tier},
        "escalate_after": live.escalate_after,
        "recover_after": live.recover_after,
        "ramp_steps": RAMP_STEPS,
        "tier_mix_per_step": {t.name: w for t, w in TIER_MIX.items()},
        "engaged_at_pressure": ramp_result["engaged_at"],
        "released_at_pressure": released_at,
        "served": ramp_result["served"],
        "dropped": ramp_result["dropped"],
        "dark_launch": {
            # Dark launch must drop NOTHING while counting the same verdicts.
            "actually_dropped": sum(dark_result["dropped"].values()),
            "would_have_dropped": {
                k.split(":", 1)[1]: v
                for k, v in dark.snapshot().items()
                if isinstance(k, str) and k.startswith("would_shed:")
            },
        },
        "decisions_per_second": throughput(LoadShedder()),
    }

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()

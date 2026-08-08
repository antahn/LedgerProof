"""Tiered load shedding: order, gradualness, dark launch, kill switch."""

from __future__ import annotations

from ledgerproof.limits.shed import DEFAULT_THRESHOLDS, LoadShedder, Tier


def drive(shedder: LoadShedder, pressure: float, times: int) -> int:
    for _ in range(times):
        shedder.observe(pressure)
    return shedder.level


def test_tiers_are_ordered_cheapest_first() -> None:
    # The order IS the design: money last.
    assert list(Tier) == [Tier.TEST_MODE, Tier.GET, Tier.POST, Tier.CRITICAL]
    assert [DEFAULT_THRESHOLDS[t] for t in Tier] == sorted(
        DEFAULT_THRESHOLDS[t] for t in Tier
    ), "a cheaper tier must never shed later than a more expensive one"


def test_nothing_sheds_when_idle() -> None:
    shedder = LoadShedder()
    drive(shedder, 0.1, 10)
    assert shedder.level == 0
    assert all(not shedder.should_shed(t).shed for t in Tier)


def test_test_mode_traffic_sheds_first() -> None:
    shedder = LoadShedder()
    drive(shedder, 0.65, 5)
    assert shedder.should_shed(Tier.TEST_MODE).shed is True
    assert shedder.should_shed(Tier.GET).shed is False
    assert shedder.should_shed(Tier.POST).shed is False
    assert shedder.should_shed(Tier.CRITICAL).shed is False


def test_critical_writes_are_protected_until_the_very_end() -> None:
    shedder = LoadShedder()
    # Sustained pressure below the CRITICAL threshold: everything else may go,
    # the money path must not.
    drive(shedder, 0.95, 30)
    assert shedder.should_shed(Tier.TEST_MODE).shed is True
    assert shedder.should_shed(Tier.GET).shed is True
    assert shedder.should_shed(Tier.POST).shed is True
    assert shedder.should_shed(Tier.CRITICAL).shed is False, (
        "critical writes must survive everything short of collapse"
    )

    drive(shedder, 0.99, 10)
    assert shedder.should_shed(Tier.CRITICAL).shed is True


def test_escalation_is_gradual_not_a_jump() -> None:
    # Pressure straight to maximum must still climb one tier at a time.
    shedder = LoadShedder()
    levels = [shedder.observe(1.0) for _ in range(shedder.escalate_after * 4)]
    assert levels == [0, 1, 1, 2, 2, 3, 3, 4], levels


def test_recovery_is_slower_than_escalation() -> None:
    shedder = LoadShedder()
    assert shedder.recover_after > shedder.escalate_after, (
        "being quick to stop shedding is how a shedder flaps"
    )
    drive(shedder, 1.0, 8)
    assert shedder.level == 4

    # Not enough calm samples yet.
    drive(shedder, 0.0, shedder.recover_after - 1)
    assert shedder.level == 4
    shedder.observe(0.0)
    assert shedder.level == 3


def test_does_not_flap_on_a_threshold_boundary() -> None:
    """Pressure oscillating across one threshold must not move the level."""
    shedder = LoadShedder()
    drive(shedder, 0.65, 5)
    assert shedder.level == 1

    for _ in range(20):
        shedder.observe(0.61)  # just above TEST_MODE
        shedder.observe(0.59)  # just below it
    assert shedder.level == 1, "alternating samples must not ratchet the level"


def test_dark_launch_records_without_dropping() -> None:
    shedder = LoadShedder(enforce=False)
    drive(shedder, 1.0, 10)
    assert shedder.level == 4

    decision = shedder.should_shed(Tier.POST)
    assert decision.shed is False, "dark launch must never actually drop traffic"
    assert decision.enforced is False

    snap = shedder.snapshot()
    assert snap["would_shed:POST"] == 1, "but it must record what it would have dropped"
    assert snap["dark_launch_allowed:POST"] == 1
    assert "shed:POST" not in snap


def test_enforcing_and_dark_launch_agree_on_what_would_shed() -> None:
    """Same pressure, same tier: only the outcome differs, never the verdict."""
    live, dark = LoadShedder(), LoadShedder(enforce=False)
    for shedder in (live, dark):
        drive(shedder, 0.92, 10)

    assert live.level == dark.level
    live_decision = live.should_shed(Tier.POST)
    dark_decision = dark.should_shed(Tier.POST)
    assert live_decision.shed is True
    assert dark_decision.shed is False
    assert live.snapshot()["would_shed:POST"] == dark.snapshot()["would_shed:POST"] == 1


def test_kill_switch_stops_everything_immediately() -> None:
    shedder = LoadShedder()
    drive(shedder, 1.0, 10)
    assert shedder.level == 4

    shedder.disable()
    assert shedder.level == 0
    for tier in Tier:
        decision = shedder.should_shed(tier)
        assert decision.shed is False
        assert decision.reason == "kill_switch"

    # And it stays off under continued pressure.
    drive(shedder, 1.0, 20)
    assert shedder.level == 0
    assert shedder.should_shed(Tier.TEST_MODE).shed is False

    shedder.enable()
    drive(shedder, 1.0, 4)
    assert shedder.level > 0

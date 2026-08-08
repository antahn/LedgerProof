"""Tiered load shedding (brief §5.5).

Under overload something must be dropped, and the only question is *what*. The
tiers below are ordered so that the traffic whose loss costs least goes first,
and fleet capacity is reserved for the one class of work that must never be
dropped: the critical write path that moves money.

Shed-first order:

    1. TEST_MODE   synthetic and exploratory traffic
    2. GET         reads; a caller can retry, and stale data beats no service
    3. POST        ordinary writes
    4. CRITICAL    the money path — shed only when the alternative is collapse

**Shed and recover GRADUALLY.** A shedder that jumps straight to its target
level flaps: it drops enough load to look healthy, immediately stops shedding,
is overwhelmed again, and oscillates — with every caller's retries synchronised
to the oscillation. So escalation moves one tier at a time, recovery moves one
tier at a time and requires *more* consecutive calm observations than
escalation required hot ones. Asymmetric on purpose: being slow to stop
shedding costs a little availability, being quick to stop shedding costs the
whole system.

**Dark launch.** `enforce=False` records exactly what *would* have been shed
without dropping anything, so the thresholds can be tuned against real traffic
before they can hurt. The counters are identical in both modes; only the
returned decision changes.

**Kill switch.** `disable()` stops all shedding immediately and unconditionally,
including dark-launch accounting. A limiter you cannot turn off during an
incident is itself an incident.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from enum import IntEnum

logger = logging.getLogger(__name__)


class Tier(IntEnum):
    """Shed-first order: lower value is shed earlier."""

    TEST_MODE = 0
    GET = 1
    POST = 2
    CRITICAL = 3


# Pressure at which each tier starts shedding. Deliberately leaves headroom
# above CRITICAL: if the money path is shedding, the system is already failing.
DEFAULT_THRESHOLDS: dict[Tier, float] = {
    Tier.TEST_MODE: 0.60,
    Tier.GET: 0.75,
    Tier.POST: 0.90,
    Tier.CRITICAL: 0.98,
}

ESCALATE_AFTER = 2  # consecutive hot observations before shedding one more tier
RECOVER_AFTER = 5  # consecutive calm ones before shedding one fewer — slower


@dataclass
class ShedDecision:
    shed: bool
    tier: Tier
    level: int
    pressure: float
    enforced: bool
    reason: str = ""


@dataclass
class LoadShedder:
    """Gradual, tiered shedding driven by a scalar pressure signal in [0, 1].

    `level` is the number of tiers currently being shed, counted from the
    cheapest: 0 sheds nothing, 1 sheds TEST_MODE, 4 sheds everything including
    the money path.
    """

    thresholds: dict[Tier, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    enforce: bool = True
    escalate_after: int = ESCALATE_AFTER
    recover_after: int = RECOVER_AFTER

    level: int = 0
    pressure: float = 0.0
    enabled: bool = True
    _hot: int = 0
    _calm: int = 0
    counts: Counter = field(default_factory=Counter)

    # ------------------------------------------------------- kill switch --

    def disable(self) -> None:
        """Kill switch: stop shedding entirely, including dark-launch counting."""
        self.enabled = False
        self.level = 0
        self._hot = self._calm = 0
        logger.warning("load shedder DISABLED by kill switch")

    def enable(self) -> None:
        self.enabled = True
        logger.warning("load shedder re-enabled at level %d", self.level)

    # ---------------------------------------------------------- pressure --

    def _target_level(self, pressure: float) -> int:
        return sum(1 for tier in Tier if pressure >= self.thresholds[tier])

    def observe(self, pressure: float) -> int:
        """Feed a pressure sample; returns the (possibly unchanged) level.

        Moves at most ONE tier per call, and only after enough consecutive
        samples agree — the whole anti-flap mechanism lives here.
        """
        self.pressure = pressure
        if not self.enabled:
            return self.level

        target = self._target_level(pressure)
        if target > self.level:
            self._hot += 1
            self._calm = 0
            if self._hot >= self.escalate_after:
                self.level += 1
                self._hot = 0
                logger.warning(
                    "shedding escalated to level %d (pressure %.2f)", self.level, pressure
                )
        elif target < self.level:
            self._calm += 1
            self._hot = 0
            if self._calm >= self.recover_after:
                self.level -= 1
                self._calm = 0
                logger.info("shedding relaxed to level %d (pressure %.2f)", self.level, pressure)
        else:
            self._hot = self._calm = 0
        return self.level

    # ---------------------------------------------------------- decision --

    def should_shed(self, tier: Tier) -> ShedDecision:
        """Would this tier be shed right now? Honours dark launch and kill switch."""
        if not self.enabled:
            return ShedDecision(
                shed=False,
                tier=tier,
                level=self.level,
                pressure=self.pressure,
                enforced=False,
                reason="kill_switch",
            )

        would_shed = int(tier) < self.level
        if would_shed:
            # Counted in BOTH modes: the point of a dark launch is to learn
            # exactly what enforcement would have cost.
            self.counts[f"would_shed:{tier.name}"] += 1
            if not self.enforce:
                self.counts[f"dark_launch_allowed:{tier.name}"] += 1
            else:
                self.counts[f"shed:{tier.name}"] += 1
        else:
            self.counts[f"allowed:{tier.name}"] += 1

        return ShedDecision(
            shed=would_shed and self.enforce,
            tier=tier,
            level=self.level,
            pressure=self.pressure,
            enforced=self.enforce,
            reason="shed_by_tier" if would_shed else "",
        )

    def snapshot(self) -> dict[str, int | float | bool]:
        """Everything a dashboard or an artifact needs, in one dict."""
        return {
            "level": self.level,
            "pressure": round(self.pressure, 4),
            "enforce": self.enforce,
            "enabled": self.enabled,
            **{k: v for k, v in sorted(self.counts.items())},
        }

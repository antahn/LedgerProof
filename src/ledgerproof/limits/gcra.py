"""GCRA rate limiter (brief §5.5).

GCRA — the Generic Cell Rate Algorithm — is a leaky-bucket variant that never
simulates a leak. Instead it stores one number per key, the **theoretical
arrival time** (TAT): the earliest moment at which the next request would be
perfectly conforming. Whether a request is allowed is then arithmetic on that
one float, which is why GCRA wins on memory and compute over a true sliding
window (which must retain every timestamp in the window).

    T   = period / rate          emission interval: the ideal spacing
    tau = T * (burst - 1)        how far ahead of schedule a caller may run
    allow  <=>  now >= tat - tau
    on allow: tat = max(now, tat) + T

With ``burst`` set to N, a cold key admits exactly N requests immediately and
then settles to one per T. (``tau = T * (burst - 1)``, not ``T * burst``:
the latter admits N+1, which is the classic off-by-one in GCRA writeups.)

**`2 req/sec` is not interchangeable with `120 req/min`.** They describe the
same average and completely different behaviour: the coarser window admits all
120 requests in the first second and then nothing for 59, which is precisely
the burst a downstream service falls over on. Rate and period are kept as
separate parameters here so the choice is explicit at every call site.

Three adaptations, all required (§5.5):

1. **A Redis lock guards the read-modify-write of the TAT.** Read-then-write
   without one is the same race the ledger has: two concurrent callers both
   read the old TAT and both allow. (A Lua script would also make this atomic;
   the lock is what this project specifies, and it keeps the fail-open /
   fail-closed split below expressible.)
2. **Float timestamps, not integers.** At 2 req/sec the emission interval is
   0.5s; integer seconds cannot represent it, and rounding turns a limiter into
   a random number generator.
3. **Redis server time (`TIME`) as the clock source**, never the application
   server's. App clocks drift apart, and two nodes disagreeing about `now` will
   disagree about the same TAT — one admitting what the other denies.

**The two failure policies point in opposite directions, on purpose:**

- A limiter **error** (Redis down, timeout, malformed state) **fails OPEN** —
  allow the request, log, alert. A rate limiter is a safety device, not a
  dependency of the thing it protects; a limiter bug must never be able to take
  down the API it guards.
- A lock **acquisition failure fails CLOSED** — deny. Failing to acquire does
  not mean Redis is broken; it means *a concurrent writer is mid-update*.
  Allowing would be exactly the double-admit the lock exists to prevent, so the
  safe answer is to shed one request.

The distinction is: an unreachable limiter knows nothing and must not block;
a contended limiter knows something specific and must not guess.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

import redis

logger = logging.getLogger(__name__)

# Long enough to cover a round trip, short enough that a killed holder cannot
# wedge a key: the lock is released on the same call that takes it.
LOCK_TTL_MS = 500
LOCK_RETRY_DELAY_S = 0.002
LOCK_ACQUIRE_TIMEOUT_S = 0.05

# Release only if we still hold it: a lock that expired mid-operation now
# belongs to someone else, and deleting it blind would free another caller's.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


@dataclass(frozen=True)
class Decision:
    """The outcome of one limiter check."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_s: float
    reason: str = ""

    @property
    def headers(self) -> dict[str, str]:
        """Standard rate-limit response headers."""
        out = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(0, self.remaining)),
        }
        if not self.allowed:
            out["Retry-After"] = str(max(1, int(self.retry_after_s + 0.999)))
        return out


class LockUnavailable(Exception):
    """A concurrent writer holds the key's lock (deny — fail closed)."""


class GCRALimiter:
    """Per-key GCRA limiter backed by Redis.

    ``rate`` requests per ``period`` seconds, admitting up to ``burst``
    back-to-back before throttling. ``fail_open`` keeps the documented policy
    configurable for tests, but production must leave it True.
    """

    def __init__(
        self,
        client: redis.Redis,
        *,
        rate: int,
        period: float = 1.0,
        burst: int = 1,
        namespace: str = "gcra",
        fail_open: bool = True,
    ) -> None:
        if rate <= 0 or period <= 0:
            raise ValueError("rate and period must be positive")
        if burst < 1:
            raise ValueError("burst must be at least 1 (1 = no burst allowance)")
        self._redis = client
        self.rate = rate
        self.period = float(period)
        self.burst = burst
        self._namespace = namespace
        self._fail_open = fail_open
        self.emission_interval = self.period / rate  # T
        self.tolerance = self.emission_interval * (burst - 1)  # tau
        # Observability: a fail-open event is invisible unless counted.
        self.fail_open_count = 0
        self.fail_closed_count = 0

    # ----------------------------------------------------------- clock --

    def _now(self) -> float:
        """Redis server time as a float. Never the local clock."""
        seconds, microseconds = self._redis.time()
        return float(seconds) + float(microseconds) / 1_000_000.0

    # ------------------------------------------------------------ lock --

    def _acquire(self, lock_key: str) -> str:
        token = str(uuid.uuid4())
        deadline = time.monotonic() + LOCK_ACQUIRE_TIMEOUT_S
        while True:
            if self._redis.set(lock_key, token, nx=True, px=LOCK_TTL_MS):
                return token
            if time.monotonic() >= deadline:
                # FAIL CLOSED: someone else is mid-update on this exact key.
                raise LockUnavailable(lock_key)
            time.sleep(LOCK_RETRY_DELAY_S)

    def _release(self, lock_key: str, token: str) -> None:
        try:
            self._redis.eval(_RELEASE, 1, lock_key, token)
        except redis.RedisError:
            logger.warning("could not release limiter lock %s; it will expire", lock_key)

    # ----------------------------------------------------------- check --

    def check(self, key: str) -> Decision:
        """Consume one unit against ``key``.

        Allowed requests advance the TAT; denied ones do not, so a caller that
        keeps hammering a throttled key does not push its own recovery further
        away.
        """
        tat_key = f"{self._namespace}:tat:{{{key}}}"
        lock_key = f"{self._namespace}:lock:{{{key}}}"

        try:
            token = self._acquire(lock_key)
        except LockUnavailable:
            self.fail_closed_count += 1
            logger.info("limiter lock busy for %s: denying (fail closed)", key)
            return Decision(
                allowed=False,
                limit=self.rate,
                remaining=0,
                retry_after_s=self.emission_interval,
                reason="lock_contended",
            )
        except redis.RedisError as exc:
            return self._fail_open_decision(key, exc)

        try:
            now = self._now()
            raw = self._redis.get(tat_key)
            tat = float(raw) if raw is not None else now
            allow_at = tat - self.tolerance

            if now < allow_at:
                return Decision(
                    allowed=False,
                    limit=self.rate,
                    remaining=0,
                    retry_after_s=allow_at - now,
                    reason="rate_limited",
                )

            new_tat = max(now, tat) + self.emission_interval
            # Expire once the key could not deny anything anyway; keeps idle
            # keys from accumulating forever.
            ttl_s = max(new_tat - now + self.tolerance, self.emission_interval)
            self._redis.set(tat_key, repr(new_tat), px=int(ttl_s * 1000) + 1000)
            return Decision(
                allowed=True,
                limit=self.rate,
                remaining=self._remaining(now, new_tat),
                retry_after_s=0.0,
            )
        except redis.RedisError as exc:
            return self._fail_open_decision(key, exc)
        except ValueError as exc:  # corrupt TAT value: treat as an outage
            return self._fail_open_decision(key, exc)
        finally:
            self._release(lock_key, token)

    def _remaining(self, now: float, tat: float) -> int:
        """How many more requests would be admitted right now."""
        if self.emission_interval <= 0:
            return self.burst
        ahead = tat - now
        return max(0, int((self.tolerance + self.emission_interval - ahead) // self.emission_interval))

    def _fail_open_decision(self, key: str, exc: Exception) -> Decision:
        if not self._fail_open:
            return Decision(
                allowed=False,
                limit=self.rate,
                remaining=0,
                retry_after_s=self.emission_interval,
                reason="limiter_error_fail_closed",
            )
        self.fail_open_count += 1
        # Loud on purpose: silently allowing everything looks identical to a
        # working limiter until the traffic that needed limiting shows up.
        logger.error("rate limiter unavailable for %s, ALLOWING (fail open): %s", key, exc)
        return Decision(
            allowed=True,
            limit=self.rate,
            remaining=self.rate,
            retry_after_s=0.0,
            reason="limiter_error_fail_open",
        )

    def reset(self, key: str) -> None:
        """Forget a key's state (tests and operational recovery)."""
        self._redis.delete(f"{self._namespace}:tat:{{{key}}}")

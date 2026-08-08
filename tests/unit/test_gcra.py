"""GCRA limiter: rate, burst, the two opposite failure policies, and the race.

These run against a REAL Redis (the algorithm's correctness depends on Redis
server time and on SET NX semantics; a fake would test the fake).
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
import redis

from ledgerproof.limits.gcra import GCRALimiter, LockUnavailable

REDIS_URL = "redis://localhost:6379/2"  # DB 2: never the dev queue or the harness


@pytest.fixture()
def client() -> redis.Redis:
    c = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        c.ping()
    except redis.RedisError:  # pragma: no cover
        pytest.skip("redis not reachable on localhost:6379")
    c.flushdb()
    return c


@pytest.fixture()
def key() -> str:
    return f"test-{uuid.uuid4()}"


def test_burst_then_throttle(client: redis.Redis, key: str) -> None:
    # burst=3 admits exactly 3 back-to-back, not 2 and not 4. tau = T*(burst-1);
    # the classic writeup bug is tau = T*burst, which admits burst+1.
    limiter = GCRALimiter(client, rate=10, period=1.0, burst=3)
    assert [limiter.check(key).allowed for _ in range(4)] == [True, True, True, False]


def test_no_burst_means_strict_spacing(client: redis.Redis, key: str) -> None:
    limiter = GCRALimiter(client, rate=10, period=1.0, burst=1)
    assert limiter.check(key).allowed is True
    assert limiter.check(key).allowed is False


def test_capacity_refills_over_time(client: redis.Redis, key: str) -> None:
    limiter = GCRALimiter(client, rate=20, period=1.0, burst=1)  # T = 50ms
    assert limiter.check(key).allowed is True
    assert limiter.check(key).allowed is False
    time.sleep(0.08)
    assert limiter.check(key).allowed is True, "one emission interval should refill one slot"


def test_denied_requests_do_not_push_recovery_further_away(
    client: redis.Redis, key: str
) -> None:
    # A caller hammering a throttled key must not extend its own penalty; only
    # allowed requests advance the TAT.
    limiter = GCRALimiter(client, rate=20, period=1.0, burst=1)
    assert limiter.check(key).allowed is True
    for _ in range(20):
        limiter.check(key)
    time.sleep(0.08)
    assert limiter.check(key).allowed is True


def test_rate_is_enforced_over_a_window(client: redis.Redis, key: str) -> None:
    limiter = GCRALimiter(client, rate=50, period=1.0, burst=1)  # 20ms spacing
    allowed = 0
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if limiter.check(key).allowed:
            allowed += 1
        time.sleep(0.002)
    # ~25 in half a second, with slack for scheduling on a loaded machine.
    assert 18 <= allowed <= 32, allowed


def test_keys_are_independent(client: redis.Redis) -> None:
    limiter = GCRALimiter(client, rate=10, period=1.0, burst=1)
    assert limiter.check("tenant-a").allowed is True
    assert limiter.check("tenant-b").allowed is True
    assert limiter.check("tenant-a").allowed is False


def test_two_req_per_sec_is_not_120_per_min(client: redis.Redis, key: str) -> None:
    """The docstring's claim, asserted: the coarse window admits the burst."""
    fine = GCRALimiter(client, rate=2, period=1.0, burst=2, namespace="fine")
    coarse = GCRALimiter(client, rate=120, period=60.0, burst=120, namespace="coarse")

    fine_admitted = sum(fine.check(key).allowed for _ in range(30))
    coarse_admitted = sum(coarse.check(key).allowed for _ in range(30))

    assert fine_admitted == 2, "2 req/sec must admit 2 instantly"
    assert coarse_admitted == 30, "120 req/min admits the whole burst at once"


# --------------------------------------------------------------------------
# The two failure policies, which point in opposite directions on purpose.
# --------------------------------------------------------------------------


class _BrokenRedis:
    """Every operation raises, as if Redis were unreachable."""

    def time(self):
        raise redis.ConnectionError("redis is down")

    def set(self, *a, **kw):
        raise redis.ConnectionError("redis is down")

    def get(self, *a, **kw):
        raise redis.ConnectionError("redis is down")

    def eval(self, *a, **kw):
        raise redis.ConnectionError("redis is down")

    def delete(self, *a, **kw):
        raise redis.ConnectionError("redis is down")


def test_limiter_error_fails_open(key: str) -> None:
    # A limiter is a safety device, not a dependency of what it protects.
    limiter = GCRALimiter(_BrokenRedis(), rate=1, period=1.0, burst=1)
    decision = limiter.check(key)
    assert decision.allowed is True
    assert decision.reason == "limiter_error_fail_open"
    assert limiter.fail_open_count == 1


def test_lock_contention_fails_closed(client: redis.Redis, key: str) -> None:
    # Failing to take the lock is not an outage: it means a concurrent writer
    # is mid-update, and allowing would be the exact double-admit the lock
    # exists to prevent.
    limiter = GCRALimiter(client, rate=100, period=1.0, burst=10)
    lock_key = f"gcra:lock:{{{key}}}"
    client.set(lock_key, "someone-else", px=2000)

    decision = limiter.check(key)
    assert decision.allowed is False
    assert decision.reason == "lock_contended"
    assert limiter.fail_closed_count == 1


def test_the_two_policies_disagree_deliberately(client: redis.Redis, key: str) -> None:
    """One artefact asserting both halves, so the asymmetry cannot drift."""
    broken = GCRALimiter(_BrokenRedis(), rate=1, period=1.0, burst=1)
    contended = GCRALimiter(client, rate=1, period=1.0, burst=1)
    client.set(f"gcra:lock:{{{key}}}", "held", px=2000)

    assert broken.check(key).allowed is True, "unreachable limiter must not block"
    assert contended.check(key).allowed is False, "contended limiter must not guess"


def test_lock_is_released_so_a_key_is_not_wedged(client: redis.Redis, key: str) -> None:
    limiter = GCRALimiter(client, rate=100, period=1.0, burst=100)
    limiter.check(key)
    assert client.get(f"gcra:lock:{{{key}}}") is None
    assert limiter.check(key).allowed is True


def test_acquire_raises_when_lock_never_frees(client: redis.Redis, key: str) -> None:
    limiter = GCRALimiter(client, rate=1, period=1.0)
    lock_key = f"gcra:lock:{{{key}}}"
    client.set(lock_key, "held", px=5000)
    with pytest.raises(LockUnavailable):
        limiter._acquire(lock_key)


# --------------------------------------------------------------------------
# The race the lock exists to prevent — the ledger's bug, in a limiter.
# --------------------------------------------------------------------------


def test_concurrent_callers_never_exceed_the_burst(client: redis.Redis, key: str) -> None:
    """32 threads race one cold key; the limit must hold exactly.

    Without the lock guarding read-modify-write, concurrent callers all read
    the same TAT and all allow — the same unguarded read-modify-write the
    chaos harness hunts in the ledger.
    """
    burst = 5
    limiter = GCRALimiter(client, rate=5, period=10.0, burst=burst)
    results: list[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(32)

    def hammer() -> None:
        start.wait()
        decision = limiter.check(key)
        with lock:
            results.append(decision.allowed)

    threads = [threading.Thread(target=hammer) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(results)
    assert len(results) == 32
    # Contended callers fail closed, so the count can be under burst but never
    # over it. Over-admitting is the bug; under-admitting is the policy.
    assert allowed <= burst, f"over-admitted {allowed} > burst {burst}"
    assert allowed >= 1, "at least one caller must win the race"


def test_headers_describe_the_decision(client: redis.Redis, key: str) -> None:
    limiter = GCRALimiter(client, rate=10, period=1.0, burst=1)
    assert "Retry-After" not in limiter.check(key).headers
    denied = limiter.check(key)
    assert denied.headers["Retry-After"] == "1"
    assert denied.headers["RateLimit-Limit"] == "10"

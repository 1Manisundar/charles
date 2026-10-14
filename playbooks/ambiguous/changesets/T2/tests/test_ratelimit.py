import pytest

from urlshortener.ratelimit import RateLimited, RateLimiter


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeTime()


def test_burst_up_to_the_limit_then_refused(clock):
    """FR-20: a client may spend its per-minute budget at once, and not one request more."""
    limiter = RateLimiter(per_minute=5, clock=clock)

    assert [limiter.check("client") for _ in range(5)] == [0.0] * 5
    wait = limiter.check("client")

    assert wait == pytest.approx(12.0)  # 60s / 5 = one token every 12 seconds


def test_budget_refills_over_time(clock):
    """FR-20: a throttled client is served again once a token has been refilled."""
    limiter = RateLimiter(per_minute=6, clock=clock)
    for _ in range(6):
        limiter.check("client")
    assert limiter.check("client") > 0

    clock.now += 10  # exactly one token at 6 per minute
    assert limiter.check("client") == 0.0
    assert limiter.check("client") > 0

    clock.now += 3600  # a long pause never banks more than one full burst
    assert [limiter.check("client") for _ in range(6)] == [0.0] * 6
    assert limiter.check("client") > 0


def test_refused_requests_do_not_extend_the_penalty(clock):
    """FR-20: hammering while throttled does not push the retry time further out."""
    limiter = RateLimiter(per_minute=1, clock=clock)
    limiter.check("client")
    first = limiter.check("client")
    clock.now += 30
    assert limiter.check("client") == pytest.approx(first - 30)


def test_clients_have_independent_budgets(clock):
    """FR-20: one noisy client does not throttle another."""
    limiter = RateLimiter(per_minute=1, clock=clock)
    assert limiter.check("noisy") == 0.0
    assert limiter.check("noisy") > 0
    assert limiter.check("quiet") == 0.0


def test_zero_disables_the_limiter(clock):
    """FR-20: a limit of 0 switches rate limiting off and keeps no state."""
    limiter = RateLimiter(per_minute=0, clock=clock)
    assert all(limiter.check("client") == 0.0 for _ in range(1000))
    assert len(limiter) == 0


def test_memory_is_bounded(clock):
    """NFR-21: tracking state never grows beyond max_keys, however many clients appear."""
    limiter = RateLimiter(per_minute=1, max_keys=100, clock=clock)
    for index in range(5000):
        limiter.check(f"client-{index}")
    assert len(limiter) == 100
    assert limiter.check("client-4999") > 0  # recent clients are still tracked
    assert limiter.check("client-0") == 0.0  # the oldest were forgotten


def test_rate_limited_error_rounds_up_to_whole_seconds():
    """FR-20: Retry-After is a whole number of seconds and never zero."""
    assert RateLimited(0.2).retry_after_seconds == 1
    assert RateLimited(11.01).retry_after_seconds == 12

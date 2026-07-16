from council_meetings.rate_limit import RateLimiter


def test_rate_limiter_allows_requests_after_window() -> None:
    now = 100.0
    limiter = RateLimiter(limit=2, window_seconds=10, clock=lambda: now)

    assert limiter.check("client") == (True, 0)
    assert limiter.check("client") == (True, 0)
    assert limiter.check("client") == (False, 10)

    now = 110.0

    assert limiter.check("client") == (True, 0)


def test_rate_limiter_bounds_client_keys() -> None:
    now = 0.0
    limiter = RateLimiter(limit=1, window_seconds=60, clock=lambda: now, max_keys=2)

    assert limiter.check("first") == (True, 0)
    assert limiter.check("second") == (True, 0)
    assert limiter.check("third") == (False, 60)
    assert limiter.check("first") == (False, 60)

    now = 60.0

    assert limiter.check("third") == (True, 0)
    assert len(limiter._requests) == 1

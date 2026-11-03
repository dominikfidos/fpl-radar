from app.ratelimit import RateLimiter


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_limiter_blocks_after_max_calls_and_frees_up_later():
    clock = Clock()
    limiter = RateLimiter(max_calls=2, window_seconds=60, clock=clock)
    assert limiter.retry_after("a") == 0
    assert limiter.retry_after("a") == 0
    assert limiter.retry_after("a") == 60  # third call inside the window: wait the full minute
    clock.now = 30
    assert limiter.retry_after("a") == 30  # half of the window is gone
    clock.now = 61
    assert limiter.retry_after("a") == 0  # the old calls expired


def test_limiter_counts_every_visitor_separately():
    limiter = RateLimiter(max_calls=1, window_seconds=60, clock=Clock())
    assert limiter.retry_after("a") == 0
    assert limiter.retry_after("b") == 0
    assert limiter.retry_after("a") > 0

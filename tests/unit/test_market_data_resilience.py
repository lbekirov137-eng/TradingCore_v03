import requests
import pytest

from api.market_data.resilience import retry_with_backoff, RateLimitExceededError, ClockSkewChecker


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error


class TestRetryWithBackoff:

    def test_succeeds_on_first_try_without_sleeping(self):
        calls = {"n": 0}
        sleeps = []

        def fn():
            calls["n"] += 1
            return "ok"

        result = retry_with_backoff(fn, sleep_fn=sleeps.append)

        assert result == "ok"
        assert calls["n"] == 1
        assert sleeps == []

    def test_retries_on_connection_error_then_succeeds(self):
        calls = {"n": 0}
        sleeps = []

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise requests.ConnectionError("simulated network failure")
            return "recovered"

        result = retry_with_backoff(fn, max_retries=5, base_delay=1.0, sleep_fn=sleeps.append)

        assert result == "recovered"
        assert calls["n"] == 3
        assert sleeps == [1.0, 2.0]  # exponential backoff

    def test_gives_up_after_max_retries_and_raises(self):
        def fn():
            raise requests.Timeout("always times out")

        with pytest.raises(requests.Timeout):
            retry_with_backoff(fn, max_retries=3, sleep_fn=lambda s: None)

    def test_429_rate_limit_retries_then_raises_specific_error(self):
        def fn():
            resp = FakeResponse(status_code=429)
            resp.raise_for_status()

        with pytest.raises(RateLimitExceededError):
            retry_with_backoff(fn, max_retries=3, sleep_fn=lambda s: None)

    def test_429_recovers_on_retry(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            resp = FakeResponse(status_code=429 if calls["n"] < 2 else 200)
            resp.raise_for_status()
            return "ok"

        result = retry_with_backoff(fn, max_retries=3, sleep_fn=lambda s: None)
        assert result == "ok"
        assert calls["n"] == 2

    def test_non_retryable_4xx_is_not_retried(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            resp = FakeResponse(status_code=404)
            resp.raise_for_status()

        with pytest.raises(requests.HTTPError):
            retry_with_backoff(fn, max_retries=5, sleep_fn=lambda s: None)

        assert calls["n"] == 1  # no retry attempted


class TestClockSkewChecker:

    def test_no_skew_when_times_match(self):
        result = ClockSkewChecker.check(server_time_ms=1_700_000_000_000, local_time_ms=1_700_000_000_000)
        assert result["skewed"] is False
        assert result["skew_seconds"] == 0.0

    def test_detects_significant_positive_skew(self):
        result = ClockSkewChecker.check(
            server_time_ms=1_700_000_000_000,
            local_time_ms=1_700_000_000_000 + 10_000,  # local clock 10s ahead
            max_skew_seconds=5.0,
        )
        assert result["skewed"] is True
        assert result["skew_seconds"] == 10.0

    def test_detects_significant_negative_skew(self):
        result = ClockSkewChecker.check(
            server_time_ms=1_700_000_000_000,
            local_time_ms=1_700_000_000_000 - 10_000,  # local clock 10s behind
            max_skew_seconds=5.0,
        )
        assert result["skewed"] is True
        assert result["skew_seconds"] == -10.0

    def test_small_skew_within_tolerance_is_not_flagged(self):
        result = ClockSkewChecker.check(
            server_time_ms=1_700_000_000_000,
            local_time_ms=1_700_000_002_000,  # 2s difference
            max_skew_seconds=5.0,
        )
        assert result["skewed"] is False

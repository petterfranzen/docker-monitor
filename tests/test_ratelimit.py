"""Unit tests for guest request limits (docker_monitor/ratelimit.py)."""
from dataclasses import dataclass

from docker_monitor.ratelimit import GuestRateLimiter, is_local, resolve_client_ip


@dataclass
class _Cfg:
    guest_rate_window_seconds: int = 600
    guest_max_requests_per_window: int = 3
    guest_max_requests_per_day: int = 10


# A genuinely routable address. Note the documentation ranges
# (203.0.113.0/24, 198.51.100.0/24) will NOT do here: Python's
# ipaddress.is_private covers the whole IANA special-purpose registry, so
# is_local() correctly treats them as non-public and exempts them.
PUBLIC = "8.8.8.8"
OTHER_PUBLIC = "1.1.1.1"


# -- client identification -----------------------------------------------


def test_forwarded_header_is_ignored_unless_proxies_are_trusted():
    """Any client can send X-Forwarded-For. Trusting it unconditionally
    would let a guest mint a fresh identity per request and walk straight
    past every limit below."""
    headers = {"x-forwarded-for": "1.2.3.4"}
    assert resolve_client_ip(headers, "8.8.4.4", trust_proxy=False) == "8.8.4.4"
    assert resolve_client_ip(headers, "8.8.4.4", trust_proxy=True) == "1.2.3.4"


def test_leftmost_forwarded_entry_is_the_client():
    headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.1, 10.0.0.2"}
    assert resolve_client_ip(headers, "10.0.0.2", trust_proxy=True) == "1.2.3.4"


def test_falls_back_when_forwarded_header_is_empty():
    assert resolve_client_ip({"x-forwarded-for": "  "}, "10.0.0.2", trust_proxy=True) == "10.0.0.2"


def test_local_address_detection():
    assert is_local("127.0.0.1")
    assert is_local("192.168.1.10")
    assert is_local("10.1.2.3")
    assert is_local("::1")
    assert not is_local(PUBLIC)
    assert not is_local("not-an-ip")
    # Documentation/reserved ranges land on the "local" side, which is the
    # right call: they are never a real remote client.
    assert is_local("203.0.113.5")


# -- limits ---------------------------------------------------------------


def test_local_callers_are_exempt():
    """The owner testing from their own LAN is not who any of this is for."""
    limiter = GuestRateLimiter(_Cfg(guest_max_requests_per_window=1))
    for _ in range(10):
        assert limiter.check("192.168.1.10", now=0.0).allowed


def test_rate_limit_trips_and_reports_a_retry_delay():
    limiter = GuestRateLimiter(_Cfg(guest_max_requests_per_window=2, guest_rate_window_seconds=600))
    assert limiter.check(PUBLIC, now=0.0).allowed
    assert limiter.check(PUBLIC, now=1.0).allowed

    decision = limiter.check(PUBLIC, now=2.0)
    assert not decision.allowed
    assert decision.retry_after_seconds > 0
    assert "too many requests" in decision.reason


def test_rate_limit_window_slides():
    limiter = GuestRateLimiter(_Cfg(guest_max_requests_per_window=1, guest_rate_window_seconds=60))
    assert limiter.check(PUBLIC, now=0.0).allowed
    assert not limiter.check(PUBLIC, now=30.0).allowed
    assert limiter.check(PUBLIC, now=61.0).allowed


def test_limits_are_per_caller():
    limiter = GuestRateLimiter(_Cfg(guest_max_requests_per_window=1))
    assert limiter.check(PUBLIC, now=0.0).allowed
    assert limiter.check(OTHER_PUBLIC, now=0.0).allowed


def test_daily_budget_stops_a_patient_caller():
    """A script that respects the rate limit but cycles a stack all day is
    exactly what the second limit exists for."""
    limiter = GuestRateLimiter(
        _Cfg(guest_max_requests_per_window=1, guest_rate_window_seconds=10, guest_max_requests_per_day=3)
    )
    for i in range(3):
        assert limiter.check(PUBLIC, now=i * 100.0).allowed

    decision = limiter.check(PUBLIC, now=400.0)
    assert not decision.allowed
    assert "daily limit" in decision.reason


def test_daily_budget_resets_after_a_day():
    limiter = GuestRateLimiter(
        _Cfg(guest_max_requests_per_window=5, guest_rate_window_seconds=10, guest_max_requests_per_day=1)
    )
    assert limiter.check(PUBLIC, now=0.0).allowed
    assert not limiter.check(PUBLIC, now=100.0).allowed
    assert limiter.check(PUBLIC, now=86401.0).allowed


def test_allowed_requests_are_counted_immediately():
    """Read-and-record in one step, so two concurrent requests can't both
    pass a check neither would pass alone."""
    limiter = GuestRateLimiter(_Cfg(guest_max_requests_per_window=1))
    limiter.check(PUBLIC, now=0.0)
    assert not limiter.check(PUBLIC, now=0.0).allowed


def test_prune_drops_callers_with_nothing_left_in_either_window():
    limiter = GuestRateLimiter(_Cfg(guest_rate_window_seconds=60))
    limiter.check(PUBLIC, now=0.0)
    limiter.prune(now=100.0)
    assert limiter._callers  # day budget still counting
    limiter.prune(now=86500.0)
    assert not limiter._callers

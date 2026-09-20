"""Guest request limits for the control API.

This is the same problem flight-tracker already solved for its public
`POST /api/agents/restart` endpoint (see `AgentController`,
`RestartRateLimiter`, `HotPollUserBudget`, `ClientIpResolver`): an
anonymous, internet-reachable endpoint that triggers real, expensive work
on someone's behalf. The shape is deliberately borrowed rather than
reinvented — separate repos and languages, so no shared code, but the same
three ideas:

1. **Per-IP request rate** — how often one caller may ask.
2. **Per-IP daily budget** — how many times a day one caller may ask,
   regardless of pacing. Stops a patient script that respects the rate
   limit from cycling a stack all day.
3. **Local callers are exempt.** The owner testing from their own LAN is
   not who any of this exists for.

All state is in memory and per-process. A restart forgets it, which is
acceptable: the hard cost ceiling is the concurrency cap plus the TTL, not
this.
"""
from __future__ import annotations

import ipaddress
import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger("docker_monitor")


def resolve_client_ip(headers, fallback: str, trust_proxy: bool) -> str:
    """The caller's address, accounting for the nginx in front of us.

    `X-Forwarded-For` is only consulted when TRUST_PROXY_HEADERS is on,
    because any client can send that header: trusting it unconditionally
    would let a guest mint a fresh identity per request and walk straight
    past every limit here. On means "something I control terminates
    connections in front of this and rewrites the header" — which is true
    for the portfolio's nginx, and false if this port is ever exposed
    directly.
    """
    if not trust_proxy:
        return fallback
    forwarded = headers.get("x-forwarded-for") or ""
    if forwarded:
        # Left-most entry is the original client; the rest are proxies.
        return forwarded.split(",")[0].strip() or fallback
    return headers.get("x-real-ip") or fallback


def is_local(ip: str) -> bool:
    """Private/loopback/link-local addresses — the owner's own network."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


@dataclass
class _CallerTrack:
    recent: list = field(default_factory=list)  # request timestamps in the rate window
    day_count: int = 0
    day_started_at: float = 0.0


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    retry_after_seconds: int = 0


ALLOWED = Decision(allowed=True)


class GuestRateLimiter:
    def __init__(self, cfg):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._callers: dict = {}

    def check(self, ip: str, now: float = None) -> Decision:
        """Whether this caller may perform a control action right now.
        Read-and-record in one step: a caller that's allowed has the
        request counted against them immediately, so two concurrent
        requests can't both pass a check neither would pass alone."""
        if is_local(ip):
            return ALLOWED

        now = time.time() if now is None else now
        window = self._cfg.guest_rate_window_seconds

        with self._lock:
            track = self._callers.setdefault(ip, _CallerTrack(day_started_at=now))

            if now - track.day_started_at >= 86400:
                track.day_count = 0
                track.day_started_at = now

            track.recent = [t for t in track.recent if now - t < window]

            if len(track.recent) >= self._cfg.guest_max_requests_per_window:
                retry = int(window - (now - track.recent[0])) + 1
                return Decision(
                    False,
                    f"too many requests — try again in {retry}s",
                    retry,
                )

            if track.day_count >= self._cfg.guest_max_requests_per_day:
                retry = int(86400 - (now - track.day_started_at)) + 1
                return Decision(
                    False,
                    "daily limit reached for this address",
                    retry,
                )

            track.recent.append(now)
            track.day_count += 1
            return ALLOWED

    def prune(self, now: float = None) -> None:
        """Drop callers with nothing left in either window, so a long-running
        process doesn't accumulate an entry per address that ever hit it."""
        now = time.time() if now is None else now
        window = self._cfg.guest_rate_window_seconds
        with self._lock:
            for ip in list(self._callers):
                track = self._callers[ip]
                idle = not [t for t in track.recent if now - t < window]
                day_over = now - track.day_started_at >= 86400
                if idle and day_over:
                    del self._callers[ip]

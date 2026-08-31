"""Alert state machine: turns a stream of ContainerSnapshots (one batch per
poll) into PROBLEM / RECOVERED events, with de-duplication so an ongoing
problem fires once (not on every poll) and, optionally, a single follow-up
event when it clears.

Deliberately keyed by container *name*, not id: `docker compose up` after a
`down`, or a restart-policy-triggered recreation, gives a container a new
id but keeps the same name, and that should still count as "the same
container" for de-dup/recovery purposes.

On the very first poll for a container we haven't seen before, we only
raise ALERT_STOPPED immediately if its restart policy says it's meant to
always be up (always / unless-stopped) — otherwise we just record a
baseline. This avoids spamming an alert for every container that happened
to already be exited before the monitor started (one-shot init containers,
old stopped experiments, etc.); from then on, any transition out of
"running" is treated as a problem, which is what actually catches a
container silently crashing or being stopped while we're watching it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .dockerstate import ContainerSnapshot

RUNNING_STATES = {"running"}
ALWAYS_ON_POLICIES = {"always", "unless-stopped"}

ALERT_STOPPED = "stopped"
ALERT_UNHEALTHY = "unhealthy"
ALERT_RESTART_LOOP = "restart_loop"

VANISHED_DETAIL = "container no longer exists on this host (removed or recreated)"


@dataclass
class Event:
    alert_type: str
    container_name: str
    recovered: bool
    detail: str
    # "critical" (container-state problems: stopped/unhealthy/restart-loop)
    # or "warning" (log-content-based problems — see logwatch.py). Drives
    # notification priority/tags in notifier.py.
    severity: str = "critical"


@dataclass
class _ContainerTrack:
    baseline_done: bool = False
    active_alerts: set = field(default_factory=set)
    restart_history: list = field(default_factory=list)  # [(timestamp, restart_count)]


class AlertEngine:
    def __init__(self, cfg):
        self._cfg = cfg
        self._tracks: dict = {}

    def evaluate(self, snapshots: list) -> list:
        now = time.time()
        events: list = []
        seen_names = set()

        for snap in snapshots:
            seen_names.add(snap.name)
            track = self._tracks.setdefault(snap.name, _ContainerTrack())

            restart_loop = self._update_restart_loop(track, snap, now)
            unhealthy = snap.health == "unhealthy"
            stopped = snap.state not in RUNNING_STATES

            current = set()
            if stopped:
                current.add(ALERT_STOPPED)
            if unhealthy:
                current.add(ALERT_UNHEALTHY)
            if restart_loop:
                current.add(ALERT_RESTART_LOOP)

            detail = self._detail(snap, restart_loop)

            if not track.baseline_done:
                track.baseline_done = True
                # Record ground truth directly rather than diffing against
                # an empty set — otherwise a container that was *already*
                # stopped when we started watching would look like a brand
                # new transition on the very next poll, even though nothing
                # changed. The one deliberate exception: a container whose
                # restart policy says it should always be running is worth
                # flagging immediately, even on the very first poll.
                if ALERT_STOPPED in current and snap.restart_policy in ALWAYS_ON_POLICIES:
                    events.append(
                        Event(ALERT_STOPPED, snap.name, recovered=False, detail=detail)
                    )
                track.active_alerts = current
            else:
                events.extend(self._diff(track, current, snap.name, detail))

        # Containers that vanished entirely since the last poll (docker rm,
        # or a container started with --rm that exited).
        for name, track in self._tracks.items():
            if name in seen_names or not track.baseline_done:
                continue
            events.extend(self._diff(track, {ALERT_STOPPED}, name, VANISHED_DETAIL))

        return events

    def _update_restart_loop(self, track: _ContainerTrack, snap: ContainerSnapshot, now: float) -> bool:
        window = self._cfg.restart_loop_window_seconds
        track.restart_history.append((now, snap.restart_count))
        track.restart_history = [
            (t, c) for (t, c) in track.restart_history if now - t <= window
        ]
        if len(track.restart_history) < 2:
            return False
        oldest_count = track.restart_history[0][1]
        return (snap.restart_count - oldest_count) >= self._cfg.restart_loop_threshold

    @staticmethod
    def _detail(snap: ContainerSnapshot, restart_loop: bool) -> str:
        parts = [f"state={snap.state}"]
        if snap.exit_code is not None:
            parts.append(f"exit_code={snap.exit_code}")
        if snap.health:
            parts.append(f"health={snap.health}")
        if snap.restart_policy:
            parts.append(f"restart_policy={snap.restart_policy}")
        if restart_loop:
            parts.append(f"restart_count={snap.restart_count}")
        return ", ".join(parts)

    @staticmethod
    def _diff(track: _ContainerTrack, current: set, name: str, detail: str) -> list:
        events = []
        for alert_type in sorted(current - track.active_alerts):
            events.append(Event(alert_type, name, recovered=False, detail=detail))
        for alert_type in sorted(track.active_alerts - current):
            events.append(Event(alert_type, name, recovered=True, detail=detail))
        track.active_alerts = current
        return events

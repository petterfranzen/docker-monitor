"""App-level phase reporting: what a container is actually *doing*, which
Docker itself has no idea about.

Docker knows "running" and, if the image defines a HEALTHCHECK, "healthy".
Neither distinguishes a flight-tracker agent that's mid-way through
populating the database from one that's sitting idle with nothing to do —
and for a demo dashboard that distinction is the whole point ("starting
up, hang on" vs. "populating data, first aircraft in a moment" vs.
"ready").

Rather than have each app grow an HTTP status endpoint for the monitor to
poll (another port, another thing to keep reachable, another failure
mode), apps emit a marker line on their own stdout:

    [phase:populating_data] global sweep 1/3

which this module parses out of the log lines logwatch.py is already
fetching each poll — no extra Docker calls, and an app that says nothing
simply has no phase, which is a perfectly honest answer.

Two rules apps must follow (see README's "Phase reporting"):

1. Emit on *transition only*, never once per loop iteration. The estimator
   refreshes every few seconds; a marker per refresh would bury the real
   logs and trip the traffic-spike detector in logwatch.py.
2. Nothing may reproduce the literal `[phase:...]` form when *reporting*
   someone else's phase — see `render_phase()` below.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

# The closed vocabulary. Anything else in a marker is ignored rather than
# passed through: this is a protocol between the apps and the dashboard,
# and an unbounded set of free-text phases would mean the UI can't know
# what any of them imply (is "warming_up" better or worse than "seeding"?).
PHASE_STARTING_UP = "starting_up"
PHASE_READY = "ready"
PHASE_POPULATING_DATA = "populating_data"
PHASE_IDLE = "idle"
PHASE_DEGRADED = "degraded"
PHASE_SHUTTING_DOWN = "shutting_down"

# Ordering used when several containers in one project report different
# phases and the project needs a single headline phase. Higher wins.
# "degraded" outranks the in-progress phases because a problem is the more
# actionable thing to show; "idle" is the floor because it's the absence of
# news.
PHASE_RANK = {
    PHASE_IDLE: 0,
    PHASE_READY: 1,
    PHASE_POPULATING_DATA: 2,
    PHASE_STARTING_UP: 3,
    PHASE_DEGRADED: 4,
    PHASE_SHUTTING_DOWN: 5,
}

KNOWN_PHASES = frozenset(PHASE_RANK)

# Human-facing wording, for the dashboard. Kept here rather than in the
# frontend so every consumer says the same thing.
PHASE_LABELS = {
    PHASE_STARTING_UP: "Starting up",
    PHASE_READY: "Ready",
    PHASE_POPULATING_DATA: "Populating data",
    PHASE_IDLE: "Idle",
    PHASE_DEGRADED: "Degraded",
    PHASE_SHUTTING_DOWN: "Shutting down",
}

_MARKER = re.compile(r"\[phase:([a-z_]+)\]\s*(.*)", re.IGNORECASE)

# A phase is a statement about a moment, not a standing fact. A container
# that said "populating_data" an hour ago and has been silent since is not
# still populating data — it's a container we know nothing current about.
DEFAULT_STALE_SECONDS = 900


@dataclass(frozen=True)
class PhaseReport:
    phase: str
    detail: str
    reported_at: float

    def age(self, now: float) -> float:
        return now - self.reported_at

    def is_stale(self, now: float, stale_seconds: float = DEFAULT_STALE_SECONDS) -> bool:
        return self.age(now) > stale_seconds

    @property
    def label(self) -> str:
        return PHASE_LABELS.get(self.phase, self.phase)


def parse_phase(line: str) -> Optional[tuple]:
    """Pull (phase, detail) out of one log line, or None if it isn't a
    phase marker. Unknown phase names are rejected rather than passed
    through — see the vocabulary note at the top of this module."""
    match = _MARKER.search(line)
    if not match:
        return None
    phase = match.group(1).lower()
    if phase not in KNOWN_PHASES:
        return None
    return phase, match.group(2).strip()


def render_phase(container_name: str, report: PhaseReport) -> str:
    """How docker-monitor writes *someone else's* phase into its own logs.

    Deliberately not the `[phase:...]` marker form. docker-monitor's own
    stdout is itself a container log stream that another instance (or a
    future version of this one) may parse, and echoing the literal marker
    would make the monitor look like it had entered the phase it's
    reporting on. This is the same class of self-referential feedback bug
    already documented in README.md, where the monitor's own alert text
    matched its own log patterns.
    """
    detail = f" detail={report.detail!r}" if report.detail else ""
    return f"phase={report.phase} container={container_name}{detail}"


def render_project_phase(project_name: str, phase: str, detail: str = "") -> str:
    """Project-level equivalent of render_phase(), same no-marker rule."""
    suffix = f" detail={detail!r}" if detail else ""
    return f"phase={phase} project={project_name}{suffix}"


class PhaseTracker:
    """Last known phase per container name, fed from log lines.

    Keyed by name rather than id for the same reason rules.py is: a
    recreated container under the same name is the same thing as far as
    anyone watching is concerned.
    """

    def __init__(self, stale_seconds: float = DEFAULT_STALE_SECONDS):
        self._stale_seconds = stale_seconds
        self._reports: dict = {}

    def observe(self, container_name: str, lines: list, now: float = None) -> Optional[PhaseReport]:
        """Feed a batch of fresh log lines. The *last* marker in the batch
        wins — a container that went starting_up → ready within one poll
        interval is ready now, not starting up."""
        now = time.time() if now is None else now
        found = None
        for line in lines:
            parsed = parse_phase(line)
            if parsed is not None:
                found = parsed
        if found is None:
            return None
        report = PhaseReport(phase=found[0], detail=found[1], reported_at=now)
        self._reports[container_name] = report
        return report

    def get(self, container_name: str, now: float = None) -> Optional[PhaseReport]:
        """Last known phase, or None if there isn't one or it has gone
        stale."""
        report = self._reports.get(container_name)
        if report is None:
            return None
        now = time.time() if now is None else now
        if report.is_stale(now, self._stale_seconds):
            return None
        return report

    def forget(self, container_name: str) -> None:
        self._reports.pop(container_name, None)

    def retain(self, container_names) -> None:
        """Drop phases for containers that are no longer around."""
        keep = set(container_names)
        for name in list(self._reports):
            if name not in keep:
                del self._reports[name]


def aggregate(reports: list) -> Optional[PhaseReport]:
    """One headline phase for a project from its containers' phases —
    the highest-ranked one, most recent breaking a tie."""
    live = [r for r in reports if r is not None]
    if not live:
        return None
    return max(live, key=lambda r: (PHASE_RANK.get(r.phase, -1), r.reported_at))

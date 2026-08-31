"""Log-content monitoring: tails each running, watched container's stdout/
stderr via the Docker Engine API's logs endpoint (docker-py's
`container.logs()` — an HTTP call to /containers/{id}/logs, not the
`docker logs` CLI) and looks for three things container/health *state*
alone can't catch:

  - "no data": the container is running (maybe even reporting healthy) but
    has stopped producing any log output for an unusually long time — often
    a sign of something silently stuck.
  - known trouble text in the logs themselves — rate-limited/throttled
    upstream by default, plus anything else configured via LOG_PATTERNS_FILE
    or the LOG_PATTERN_*_ENABLED toggles.
  - a sudden spike in log line rate vs. the container's own recent
    baseline — generic, no per-app configuration needed.

Implemented as periodic incremental fetches (since=<checkpoint>) each poll
cycle, not a long-lived streaming connection: this fits the same simple
poll-loop model main.py already uses for container state, and avoids a
background thread + reconnect/backoff per container. Still entirely via
the Engine API (docker-py), never a subprocess call to the `docker` CLI.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .rules import Event

logger = logging.getLogger("docker_monitor")

ALERT_NO_DATA = "log:no_data"
ALERT_TRAFFIC_SPIKE = "log:traffic_spike"


@dataclass(frozen=True)
class LogPattern:
    label: str
    severity: str  # "warning" or "critical"
    regex: object  # compiled re.Pattern
    containers: tuple = ()  # empty = applies to every watched container

    def applies_to(self, name: str) -> bool:
        return not self.containers or any(
            fnmatch.fnmatch(name, pat) for pat in self.containers
        )


def _default_patterns(cfg) -> list:
    patterns = []
    if cfg.log_pattern_rate_limited_enabled:
        # Deliberately does NOT match a bare "429": found live, during this
        # project's own testing, that a plain \b429\b regex reliably
        # false-positives on ordinary chatty logs — it matched
        # docker-monitor's own log line "10:01:22,429 DEBUG ..." on the
        # *milliseconds field of its own timestamp* within seconds of
        # running, and would just as easily match a port number, PID, or
        # container-id fragment in any other container's logs. "429" alone
        # is too short and too common a numeric token in unstructured text
        # to be a safe generic default; the phrases below are what
        # rate-limit/backoff logging actually tends to say and don't share
        # that collision risk. A human who wants to match a bare status
        # code for their own app's specific log format can add a tighter,
        # context-scoped pattern (e.g. requiring "429" near "http" or
        # "status") via LOG_PATTERNS_FILE — see README.
        patterns.append(
            LogPattern(
                "rate_limited",
                "warning",
                re.compile(
                    r"rate[ -]?limit|throttl|too many requests|backing off",
                    re.IGNORECASE,
                ),
            )
        )
    if cfg.log_pattern_generic_error_enabled:
        # Off by default — noisy on plenty of apps that log the word
        # "error" routinely without anything actually being wrong.
        patterns.append(
            LogPattern(
                "generic_error",
                "critical",
                re.compile(r"\b(error|exception|fatal)\b", re.IGNORECASE),
            )
        )
    return patterns


def _load_patterns_file(path: str) -> list:
    """Optional JSON file of additional patterns, e.g.:
    [
      {"label": "db_conn_lost", "severity": "critical",
       "pattern": "connection refused|connection reset",
       "containers": ["flight-tracker-backend-*"]}
    ]
    "containers" is optional (omit to apply to every watched container);
    "enabled": false skips an entry without deleting it.
    """
    entries = json.loads(Path(path).read_text())
    patterns = []
    for entry in entries:
        if entry.get("enabled", True) is False:
            continue
        patterns.append(
            LogPattern(
                entry["label"],
                entry.get("severity", "warning"),
                re.compile(entry["pattern"], re.IGNORECASE),
                tuple(entry.get("containers", [])),
            )
        )
    return patterns


@dataclass
class _ContainerLogTrack:
    checkpoint_ts: float
    first_seen_ts: float
    last_activity_ts: float
    last_seen_line_ts: str = ""
    rate_history: list = field(default_factory=list)  # [(poll_ts, elapsed_s, line_count, was_over_threshold)]
    active_patterns: set = field(default_factory=set)
    no_data_active: bool = False
    traffic_candidate: bool = False
    traffic_active: bool = False


class LogWatcher:
    def __init__(self, cfg):
        self._cfg = cfg
        self._tracks: dict = {}
        self._patterns = _default_patterns(cfg)
        if cfg.log_patterns_file:
            self._patterns += _load_patterns_file(cfg.log_patterns_file)

    def evaluate(self, running_watched: list, now: float = None) -> list:
        """running_watched: list of (docker Container, ContainerSnapshot)
        pairs for currently-running, watched containers only — log-content
        monitoring only makes sense while something is actually running."""
        now = time.time() if now is None else now
        events: list = []
        seen_names = set()

        for container, snap in running_watched:
            seen_names.add(snap.name)
            track = self._tracks.get(snap.name)
            if track is None:
                # First sighting: start the checkpoint at "now" so we don't
                # fetch/alert on a container's entire pre-existing log
                # history, and seed last_activity_ts at "now" too so
                # no-data detection has a sane starting point (effectively
                # a grace period) instead of firing on containers we've
                # simply never checked before.
                self._tracks[snap.name] = _ContainerLogTrack(
                    checkpoint_ts=now, first_seen_ts=now, last_activity_ts=now
                )
                continue

            lines = self._fetch_new_lines(container, track, now)
            if lines:
                track.last_activity_ts = now

            events.extend(self._evaluate_patterns(track, snap.name, lines))
            events.extend(self._evaluate_no_data(track, snap.name, now))
            events.extend(self._evaluate_traffic(track, snap.name, now, len(lines)))

        # Containers no longer running/watched: drop tracking. Any
        # outstanding log-only alert for them is implicitly moot (the
        # container-state engine already reports it as stopped, which is
        # the more important signal); a fresh baseline starts if/when a
        # container by this name reappears.
        for name in list(self._tracks):
            if name not in seen_names:
                del self._tracks[name]

        return events

    def _fetch_new_lines(self, container, track: _ContainerLogTrack, now: float) -> list:
        try:
            raw = container.logs(
                since=int(track.checkpoint_ts),
                until=int(now) + 1,
                timestamps=True,
                stdout=True,
                stderr=True,
            )
        except Exception:
            logger.exception("Failed to fetch logs for %s", container.name)
            return []
        track.checkpoint_ts = now

        lines = []
        for raw_line in raw.decode("utf-8", errors="replace").splitlines():
            if not raw_line:
                continue
            # Docker prefixes each line with an RFC3339Nano timestamp when
            # timestamps=True (e.g. "2026-08-31T09:20:00.123456789Z text").
            # `since` only has second granularity, so the same line can be
            # returned twice across polls near a second boundary — compare
            # the (lexicographically sortable, same fixed format) timestamp
            # strings to drop anything already processed.
            ts_str, _, text = raw_line.partition(" ")
            if ts_str <= track.last_seen_line_ts:
                continue
            track.last_seen_line_ts = ts_str
            lines.append(text)
        return lines

    def _evaluate_patterns(self, track: _ContainerLogTrack, name: str, lines: list) -> list:
        current = {}
        for pattern in self._patterns:
            if not pattern.applies_to(name):
                continue
            for line in lines:
                if pattern.regex.search(line):
                    current[pattern.label] = (pattern, line)
                    break

        events = []
        for label, (pattern, line) in current.items():
            if label not in track.active_patterns:
                events.append(
                    Event(
                        f"log:{label}",
                        name,
                        recovered=False,
                        detail=f"matched: {line!r}",
                        severity=pattern.severity,
                    )
                )
        for label in track.active_patterns - current.keys():
            pattern = next((p for p in self._patterns if p.label == label), None)
            severity = pattern.severity if pattern else "warning"
            events.append(
                Event(
                    f"log:{label}",
                    name,
                    recovered=True,
                    detail="pattern no longer matching recent log output",
                    severity=severity,
                )
            )
        track.active_patterns = set(current.keys())
        return events

    def _evaluate_no_data(self, track: _ContainerLogTrack, name: str, now: float) -> list:
        if any(fnmatch.fnmatch(name, pat) for pat in self._cfg.no_data_exclude):
            return []

        idle_seconds = now - track.last_activity_ts
        is_idle = idle_seconds > self._cfg.no_data_idle_seconds

        events = []
        if is_idle and not track.no_data_active:
            track.no_data_active = True
            events.append(
                Event(
                    ALERT_NO_DATA,
                    name,
                    recovered=False,
                    detail=(
                        f"no new log output for {int(idle_seconds)}s "
                        f"(threshold {self._cfg.no_data_idle_seconds}s)"
                    ),
                    severity="warning",
                )
            )
        elif not is_idle and track.no_data_active:
            track.no_data_active = False
            events.append(
                Event(ALERT_NO_DATA, name, recovered=True, detail="log output resumed", severity="warning")
            )
        return events

    def _evaluate_traffic(self, track: _ContainerLogTrack, name: str, now: float, line_count: int) -> list:
        last_poll_ts = track.rate_history[-1][0] if track.rate_history else track.first_seen_ts
        elapsed = max(now - last_poll_ts, 0.001)
        current_rate = line_count / elapsed  # lines/sec

        events: list = []
        grace_elapsed = now - track.first_seen_ts >= self._cfg.traffic_grace_period_seconds

        # Baseline is built only from *prior* samples that weren't
        # themselves flagged as over-threshold at the time — otherwise an
        # ongoing spike gradually drags its own baseline upward each poll
        # (or, worse, looks like it "recovered" the moment the rolling
        # window's average catches up), instead of the alert reflecting a
        # real return to the container's normal, pre-spike rate.
        normal_samples = [r for r in track.rate_history if not r[3]]
        have_baseline = grace_elapsed and len(normal_samples) >= self._cfg.traffic_min_baseline_samples

        over_threshold = False
        if have_baseline:
            baseline_lines = sum(c for _, _, c, _ in normal_samples)
            baseline_elapsed = sum(e for _, e, c, _ in normal_samples) or 1.0
            baseline_rate = baseline_lines / baseline_elapsed  # lines/sec

            over_threshold = (
                current_rate > baseline_rate * self._cfg.traffic_spike_multiplier
                and current_rate * 60 >= self._cfg.traffic_min_rate_lines_per_min
            )

            if over_threshold:
                if track.traffic_candidate and not track.traffic_active:
                    track.traffic_active = True
                    events.append(
                        Event(
                            ALERT_TRAFFIC_SPIKE,
                            name,
                            recovered=False,
                            detail=(
                                f"log rate {current_rate * 60:.1f} lines/min vs. baseline "
                                f"{baseline_rate * 60:.1f} lines/min "
                                f"({self._cfg.traffic_spike_multiplier}x threshold, sustained 2+ polls)"
                            ),
                            severity="warning",
                        )
                    )
                track.traffic_candidate = True
            else:
                if track.traffic_active:
                    track.traffic_active = False
                    events.append(
                        Event(ALERT_TRAFFIC_SPIKE, name, recovered=True, detail="log rate back to baseline", severity="warning")
                    )
                track.traffic_candidate = False

        track.rate_history.append((now, elapsed, line_count, over_threshold))
        window = self._cfg.traffic_baseline_window_seconds
        track.rate_history = [r for r in track.rate_history if now - r[0] <= window]

        return events

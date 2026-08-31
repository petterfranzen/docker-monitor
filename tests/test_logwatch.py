"""Unit tests for log-content monitoring. Uses a FakeContainer stub (no real
Docker daemon) whose `.logs()` returns one canned batch of lines per call,
so each test controls exactly what "new log output" looks like on each
simulated poll.
"""
from types import SimpleNamespace

from docker_monitor.dockerstate import ContainerSnapshot
from docker_monitor.logwatch import ALERT_NO_DATA, ALERT_TRAFFIC_SPIKE, LogWatcher


class FakeContainer:
    """Each call to .logs() pops the next batch of plain-text lines and
    returns them Docker-log-formatted (RFC3339Nano timestamp + space +
    text), with a strictly increasing timestamp per line so the watcher's
    de-dup-by-timestamp logic behaves like it would against a real daemon."""

    def __init__(self, name, batches):
        self.name = name
        self._batches = list(batches)
        self._counter = 0

    def logs(self, since=None, until=None, timestamps=True, stdout=True, stderr=True):
        lines = self._batches.pop(0) if self._batches else []
        out_lines = []
        for line in lines:
            self._counter += 1
            ts = f"2026-01-01T00:00:{self._counter:05d}.000000000Z"
            out_lines.append(f"{ts} {line}")
        return ("\n".join(out_lines) + ("\n" if out_lines else "")).encode("utf-8")


def make_cfg(**overrides):
    defaults = dict(
        log_pattern_rate_limited_enabled=True,
        log_pattern_generic_error_enabled=False,
        log_patterns_file="",
        no_data_idle_seconds=600,
        no_data_exclude=[],
        traffic_baseline_window_seconds=1800,
        traffic_spike_multiplier=5,
        traffic_min_baseline_samples=3,
        traffic_grace_period_seconds=0,
        traffic_min_rate_lines_per_min=2,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def snap(name="c1", state="running"):
    return ContainerSnapshot(
        id=f"id-{name}",
        name=name,
        image="img",
        state=state,
        exit_code=None,
        health=None,
        restart_policy="",
        restart_count=0,
        labels={},
    )


def test_first_sighting_establishes_baseline_with_no_events():
    watcher = LogWatcher(make_cfg())
    container = FakeContainer("c1", batches=[["hello"]])
    events = watcher.evaluate([(container, snap())], now=1000.0)
    assert events == []


def test_rate_limited_pattern_fires_and_recovers():
    watcher = LogWatcher(make_cfg())
    # First evaluate() call for a container never fetches (it just
    # establishes the baseline checkpoint) — so these batches line up with
    # the *second* call onward.
    container = FakeContainer(
        "c1",
        batches=[
            ["HTTP 429 Too Many Requests from upstream"],  # poll 2: pattern appears
            ["still backing off"],  # poll 3: different line, same pattern label -> no repeat
            ["all clear, normal operation"],  # poll 4: pattern gone -> recovered
        ],
    )
    s = snap()
    assert watcher.evaluate([(container, s)], now=1000.0) == []

    events = watcher.evaluate([(container, s)], now=1010.0)
    assert len(events) == 1
    assert events[0].alert_type == "log:rate_limited"
    assert events[0].severity == "warning"
    assert events[0].recovered is False

    # Still matching next poll: de-duplicated, no repeat.
    assert watcher.evaluate([(container, s)], now=1020.0) == []

    events = watcher.evaluate([(container, s)], now=1030.0)
    assert len(events) == 1
    assert events[0].alert_type == "log:rate_limited"
    assert events[0].recovered is True


def test_no_data_fires_after_idle_threshold_and_recovers():
    cfg = make_cfg(no_data_idle_seconds=100)
    watcher = LogWatcher(cfg)
    # First evaluate() call never fetches (baseline only), so these three
    # batches correspond to the second, third, and fourth calls below.
    container = FakeContainer("c1", batches=[["init"], [], ["back again"]])
    s = snap()

    assert watcher.evaluate([(container, s)], now=0.0) == []
    assert watcher.evaluate([(container, s)], now=10.0) == []  # "init" seen -> activity at t=10

    events = watcher.evaluate([(container, s)], now=150.0)  # 140s idle since t=10 -> fires
    assert len(events) == 1
    assert events[0].alert_type == ALERT_NO_DATA
    assert events[0].severity == "warning"
    assert events[0].recovered is False

    events = watcher.evaluate([(container, s)], now=160.0)  # "back again" -> recovered
    assert len(events) == 1
    assert events[0].alert_type == ALERT_NO_DATA
    assert events[0].recovered is True


def test_no_data_excluded_container_never_alerts():
    cfg = make_cfg(no_data_idle_seconds=10, no_data_exclude=["quiet-*"])
    watcher = LogWatcher(cfg)
    container = FakeContainer("quiet-db", batches=[["init"], [], [], []])
    s = snap(name="quiet-db")

    assert watcher.evaluate([(container, s)], now=0.0) == []
    assert watcher.evaluate([(container, s)], now=100.0) == []
    assert watcher.evaluate([(container, s)], now=200.0) == []


def test_traffic_spike_requires_two_consecutive_polls_over_threshold():
    cfg = make_cfg(
        traffic_min_baseline_samples=2,
        traffic_spike_multiplier=5,
        traffic_min_rate_lines_per_min=1,
        traffic_grace_period_seconds=0,
    )
    watcher = LogWatcher(cfg)
    # First evaluate() call never fetches (baseline only); these 5 batches
    # correspond to the 5 fetch calls made by the 5 subsequent evaluate()
    # calls below (t=10..50).
    container = FakeContainer(
        "c1",
        batches=[
            ["l"],  # normal baseline sample 1
            ["l"],  # normal baseline sample 2
            ["l"] * 50,  # spike candidate (poll 1 over threshold)
            ["l"] * 50,  # spike confirmed (poll 2 over threshold -> fires)
            ["l"],  # back to normal -> recovered
        ],
    )
    s = snap()
    times = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0]
    all_events = []
    for t in times:
        all_events.append(watcher.evaluate([(container, s)], now=t))

    fired = [e for batch in all_events for e in batch if e.alert_type == ALERT_TRAFFIC_SPIKE]
    assert len(fired) == 2
    assert fired[0].recovered is False
    assert fired[0].severity == "warning"
    assert fired[1].recovered is True


def test_dropped_container_resets_tracking():
    watcher = LogWatcher(make_cfg())
    container = FakeContainer("c1", batches=[["init"]])
    s = snap()
    watcher.evaluate([(container, s)], now=0.0)
    assert "c1" in watcher._tracks

    # Not in this poll's watched/running list anymore.
    watcher.evaluate([], now=10.0)
    assert "c1" not in watcher._tracks

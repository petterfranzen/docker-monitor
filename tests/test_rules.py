"""Unit tests for the alert state machine. All synthetic — no real Docker
daemon involved, which is the point of keeping rules.py independent from
dockerstate.py's Docker SDK calls.
"""
from types import SimpleNamespace

from docker_monitor.dockerstate import ContainerSnapshot
from docker_monitor.rules import (
    ALERT_RESTART_LOOP,
    ALERT_STOPPED,
    ALERT_UNHEALTHY,
    AlertEngine,
)


def make_cfg(**overrides):
    defaults = dict(restart_loop_threshold=3, restart_loop_window_seconds=300)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def snap(
    name="c1",
    state="running",
    exit_code=None,
    health=None,
    restart_policy="",
    restart_count=0,
):
    return ContainerSnapshot(
        id=f"id-{name}",
        name=name,
        image="img",
        state=state,
        exit_code=exit_code,
        health=health,
        restart_policy=restart_policy,
        restart_count=restart_count,
        labels={},
    )


def test_no_alert_on_baseline_for_already_stopped_container_without_policy():
    engine = AlertEngine(make_cfg())
    events = engine.evaluate([snap(state="exited", exit_code=1)])
    assert events == []


def test_no_alert_on_second_poll_for_container_still_stopped_since_baseline():
    # Regression: a container already stopped (no restart policy) when
    # monitoring starts must not look like a fresh transition on the next
    # poll just because it's still stopped.
    engine = AlertEngine(make_cfg())
    assert engine.evaluate([snap(name="c1", state="created", exit_code=0)]) == []
    assert engine.evaluate([snap(name="c1", state="created", exit_code=0)]) == []
    assert engine.evaluate([snap(name="c1", state="created", exit_code=0)]) == []


def test_baseline_alerts_immediately_for_always_restart_policy_stopped():
    engine = AlertEngine(make_cfg())
    events = engine.evaluate(
        [snap(state="exited", exit_code=1, restart_policy="always")]
    )
    assert len(events) == 1
    assert events[0].alert_type == ALERT_STOPPED
    assert events[0].recovered is False


def test_running_then_stopped_fires_problem_once_then_recovers():
    engine = AlertEngine(make_cfg())

    # Baseline: running, no alert.
    assert engine.evaluate([snap(state="running")]) == []

    # Stops: one PROBLEM event.
    events = engine.evaluate([snap(state="exited", exit_code=137)])
    assert len(events) == 1
    assert events[0].alert_type == ALERT_STOPPED
    assert events[0].recovered is False

    # Still stopped next poll: no repeat (de-duplicated).
    assert engine.evaluate([snap(state="exited", exit_code=137)]) == []

    # Comes back: one RECOVERED event.
    events = engine.evaluate([snap(state="running")])
    assert len(events) == 1
    assert events[0].alert_type == ALERT_STOPPED
    assert events[0].recovered is True


def test_unhealthy_transition_fires_and_recovers():
    engine = AlertEngine(make_cfg())
    assert engine.evaluate([snap(health="healthy")]) == []

    events = engine.evaluate([snap(health="unhealthy")])
    assert [e.alert_type for e in events] == [ALERT_UNHEALTHY]
    assert events[0].recovered is False

    # No repeat while still unhealthy.
    assert engine.evaluate([snap(health="unhealthy")]) == []

    events = engine.evaluate([snap(health="healthy")])
    assert [e.alert_type for e in events] == [ALERT_UNHEALTHY]
    assert events[0].recovered is True


def test_restart_loop_detected_when_count_climbs_within_window():
    engine = AlertEngine(make_cfg(restart_loop_threshold=3))
    assert engine.evaluate([snap(restart_count=0)]) == []
    assert engine.evaluate([snap(restart_count=1)]) == []
    assert engine.evaluate([snap(restart_count=2)]) == []
    events = engine.evaluate([snap(restart_count=3)])
    assert [e.alert_type for e in events] == [ALERT_RESTART_LOOP]
    assert events[0].recovered is False


def test_restart_loop_not_flagged_below_threshold():
    engine = AlertEngine(make_cfg(restart_loop_threshold=5))
    for count in range(4):
        assert engine.evaluate([snap(restart_count=count)]) == []


def test_container_vanishing_is_treated_as_stopped():
    engine = AlertEngine(make_cfg())
    assert engine.evaluate([snap(name="c1", state="running")]) == []

    # c1 no longer appears at all (docker rm, or --rm exit).
    events = engine.evaluate([])
    assert len(events) == 1
    assert events[0].container_name == "c1"
    assert events[0].alert_type == ALERT_STOPPED
    assert events[0].recovered is False

    # Reappears under the same name (e.g. recreated with a new id).
    events = engine.evaluate([snap(name="c1", state="running")])
    assert len(events) == 1
    assert events[0].recovered is True


def test_multiple_containers_tracked_independently():
    engine = AlertEngine(make_cfg())
    assert engine.evaluate([snap(name="a"), snap(name="b")]) == []

    events = engine.evaluate([snap(name="a", state="exited"), snap(name="b")])
    assert len(events) == 1
    assert events[0].container_name == "a"

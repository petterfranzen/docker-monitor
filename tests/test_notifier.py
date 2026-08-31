"""Unit tests for the ntfy priority/tag mapping — pure function, no network."""
from docker_monitor.notifier import _priority_and_tags
from docker_monitor.rules import Event


def test_critical_problem_is_high_priority():
    event = Event("stopped", "c1", recovered=False, detail="d", severity="critical")
    priority, tags = _priority_and_tags(event)
    assert priority == 5
    assert tags == ["rotating_light"]


def test_warning_problem_is_lower_priority():
    event = Event("log:rate_limited", "c1", recovered=False, detail="d", severity="warning")
    priority, tags = _priority_and_tags(event)
    assert priority == 3
    assert tags == ["warning"]


def test_critical_recovery_is_lower_priority_than_the_problem():
    event = Event("stopped", "c1", recovered=True, detail="d", severity="critical")
    priority, tags = _priority_and_tags(event)
    assert priority == 3
    assert tags == ["white_check_mark"]


def test_warning_recovery_is_lowest_priority():
    event = Event("log:no_data", "c1", recovered=True, detail="d", severity="warning")
    priority, tags = _priority_and_tags(event)
    assert priority == 2
    assert tags == ["white_check_mark"]

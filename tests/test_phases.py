"""Unit tests for the app-phase protocol (docker_monitor/phases.py)."""
from docker_monitor import phases
from docker_monitor.phases import PhaseReport, PhaseTracker


def test_parses_marker_with_detail():
    assert phases.parse_phase("2026-09-20 INFO [phase:populating_data] global sweep 1/3") == (
        "populating_data",
        "global sweep 1/3",
    )


def test_parses_marker_without_detail():
    assert phases.parse_phase("[phase:ready]") == ("ready", "")


def test_ignores_ordinary_lines():
    assert phases.parse_phase("INFO starting up the thing") is None
    assert phases.parse_phase("phase: populating_data") is None


def test_rejects_phases_outside_the_vocabulary():
    """An app inventing its own phase name gets ignored rather than passed
    through — the dashboard can't rank a phase it's never heard of."""
    assert phases.parse_phase("[phase:warming_up] almost there") is None


def test_rendered_phase_is_not_itself_a_marker():
    """The no-echo rule: docker-monitor reporting someone else's phase must
    not produce a line that parses back as a phase marker, or a monitor
    watching a monitor attributes the phase to the wrong container. This is
    the same self-referential feedback shape as the rate-limit false
    positive already documented in the README."""
    line = phases.render_phase("ft_agent", PhaseReport("populating_data", "sweep 1/3", 0))
    assert phases.parse_phase(line) is None

    project_line = phases.render_project_phase("flight-tracker", "degraded", "OpenSky 429")
    assert phases.parse_phase(project_line) is None


def test_last_marker_in_a_batch_wins():
    """A container that went starting_up -> ready inside one poll interval
    is ready, not starting up."""
    tracker = PhaseTracker()
    tracker.observe(
        "api",
        ["[phase:starting_up] boot", "doing things", "[phase:ready] listening on 8080"],
        now=100.0,
    )
    assert tracker.get("api", now=100.0).phase == "ready"


def test_phase_without_a_marker_leaves_the_previous_one_standing():
    tracker = PhaseTracker()
    tracker.observe("api", ["[phase:populating_data] sweep"], now=100.0)
    tracker.observe("api", ["just some ordinary logging"], now=110.0)
    assert tracker.get("api", now=110.0).phase == "populating_data"


def test_stale_phases_are_not_reported():
    """A phase is a statement about a moment. An hour-old "populating_data"
    from a container that's said nothing since is not current fact."""
    tracker = PhaseTracker(stale_seconds=60)
    tracker.observe("api", ["[phase:populating_data] sweep"], now=100.0)
    assert tracker.get("api", now=140.0) is not None
    assert tracker.get("api", now=200.0) is None


def test_retain_drops_vanished_containers():
    tracker = PhaseTracker()
    tracker.observe("api", ["[phase:ready]"], now=100.0)
    tracker.observe("agent", ["[phase:idle]"], now=100.0)
    tracker.retain(["api"])
    assert tracker.get("api", now=100.0) is not None
    assert tracker.get("agent", now=100.0) is None


def test_aggregate_picks_the_highest_ranked_phase():
    """A project whose api is ready but whose agent is populating shows as
    populating — that's the interesting half."""
    reports = [
        PhaseReport("ready", "", 100.0),
        PhaseReport("populating_data", "sweep", 100.0),
        PhaseReport("idle", "", 100.0),
    ]
    assert phases.aggregate(reports).phase == "populating_data"


def test_aggregate_prefers_a_problem_over_progress():
    reports = [PhaseReport("populating_data", "", 100.0), PhaseReport("degraded", "429", 100.0)]
    assert phases.aggregate(reports).phase == "degraded"


def test_aggregate_ignores_containers_with_no_phase():
    assert phases.aggregate([None, None]) is None
    assert phases.aggregate([None, PhaseReport("ready", "", 1.0)]).phase == "ready"


def test_aggregate_breaks_ties_by_recency():
    older = PhaseReport("populating_data", "first", 100.0)
    newer = PhaseReport("populating_data", "second", 200.0)
    assert phases.aggregate([older, newer]).detail == "second"

"""Unit tests for project grouping and the API's project view
(docker_monitor/projects.py)."""
import json

from docker_monitor.dockerstate import LABEL_PROJECT, LABEL_SERVICE, ContainerSnapshot
from docker_monitor.phases import PhaseTracker
from docker_monitor.projects import (
    STATE_PARTIAL,
    STATE_RUNNING,
    STATE_STOPPED,
    STATE_STOPPING,
    ProjectConfig,
    build_views,
    group_by_project,
    load_registry,
)


def _snap(name, project=None, service="", state="running", health=None):
    labels = {}
    if project:
        labels[LABEL_PROJECT] = project
        labels[LABEL_SERVICE] = service or name
    return ContainerSnapshot(
        id=name,
        name=name,
        image="img",
        state=state,
        exit_code=None,
        health=health,
        restart_policy="",
        restart_count=0,
        labels=labels,
    )


# -- grouping ------------------------------------------------------------


def test_groups_containers_by_compose_label():
    snaps = [
        _snap("ft_api", "flight-tracker", "backend-api"),
        _snap("ft_db", "flight-tracker", "db"),
        _snap("dp_api", "dinner-planner", "backend"),
    ]
    grouped = group_by_project(snaps)
    assert sorted(grouped) == ["dinner-planner", "flight-tracker"]
    assert len(grouped["flight-tracker"]) == 2


def test_containers_without_a_compose_label_are_not_in_any_project():
    """A plain `docker run` container is still watched for alerting, but it
    isn't a project and can't be controlled as one."""
    assert group_by_project([_snap("stray")]) == {}


# -- registry ------------------------------------------------------------


def test_registry_round_trip(tmp_path):
    path = tmp_path / "projects.json"
    path.write_text(
        json.dumps(
            {
                "flight-tracker": {
                    "display_name": "Flight Tracker",
                    "compose_file": "/stacks/ft/docker-compose.yml",
                    "guest_controllable": True,
                    "default_ttl_minutes": 45,
                }
            }
        )
    )
    registry = load_registry(str(path))
    config = registry["flight-tracker"]
    assert config.display_name == "Flight Tracker"
    assert config.guest_startable
    assert config.default_ttl_minutes == 45


def test_unreadable_registry_is_not_fatal(tmp_path):
    """Alerting predates project control and must keep working even if the
    registry is missing or broken."""
    assert load_registry(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert load_registry(str(bad)) == {}


def test_unknown_registry_keys_are_ignored_not_fatal(tmp_path):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"p": {"display_name": "P", "typo_key": 1}}))
    assert load_registry(str(path))["p"].display_name == "P"


def test_underscore_keys_are_treated_as_comments(tmp_path):
    """JSON has no comment syntax and the registry file needs explaining —
    see projects.example.json."""
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"_comment": ["not a project"], "p": {"display_name": "P"}}))
    assert list(load_registry(str(path))) == ["p"]


def test_project_without_a_compose_file_is_not_controllable():
    config = ProjectConfig(name="p", guest_controllable=True)
    assert not config.controllable
    assert not config.guest_startable


def test_always_on_projects_are_never_guest_startable():
    config = ProjectConfig(
        name="p", compose_file="/x.yml", guest_controllable=True, always_on=True
    )
    assert config.controllable
    assert not config.guest_startable


def test_ttl_is_clamped_to_the_configured_maximum():
    config = ProjectConfig(name="p", default_ttl_minutes=60, max_ttl_minutes=120)
    assert config.clamp_ttl(None) == 60
    assert config.clamp_ttl(0) == 60
    assert config.clamp_ttl(-5) == 60
    assert config.clamp_ttl(30) == 30
    assert config.clamp_ttl(9999) == 120


# -- views ---------------------------------------------------------------


def test_state_aggregates_across_containers():
    registry = {}
    all_up = build_views([_snap("a", "p"), _snap("b", "p")], registry)[0]
    assert all_up.state == STATE_RUNNING

    mixed = build_views([_snap("a", "p"), _snap("b", "p", state="exited")], registry)[0]
    assert mixed.state == STATE_PARTIAL

    all_down = build_views([_snap("a", "p", state="exited")], registry)[0]
    assert all_down.state == STATE_STOPPED


def test_registry_project_with_no_containers_is_listed_as_stopped():
    """The case that makes the registry necessary at all: a fully-down
    project has no containers to read labels off, but must still be
    startable."""
    registry = {"flight-tracker": ProjectConfig(name="flight-tracker", compose_file="/x.yml")}
    views = build_views([], registry)
    assert [v.name for v in views] == ["flight-tracker"]
    assert views[0].state == STATE_STOPPED
    assert views[0].controllable


def test_project_discovered_on_the_host_is_visible_but_not_controllable():
    """The main safety property: being listed is not the same as being
    launchable. An unrelated stack on the NAS can never be started or
    stopped through this API."""
    view = build_views([_snap("x", "someone-elses-stack")], {})[0]
    assert view.known is False
    assert view.controllable is False
    assert view.guest_controllable is False


def test_busy_state_overrides_the_container_states_underneath():
    """Mid-stop, the UI should say "stopping", not flicker through
    "partial" as containers go down one by one."""
    view = build_views(
        [_snap("a", "p"), _snap("b", "p", state="exited")],
        {},
        busy_projects={"p": STATE_STOPPING},
    )[0]
    assert view.state == STATE_STOPPING
    assert view.busy is True


def test_phase_surfaces_on_the_project_and_its_containers():
    tracker = PhaseTracker()
    tracker.observe("ft_agent", ["[phase:populating_data] global sweep 1/3"], now=100.0)
    tracker.observe("ft_api", ["[phase:ready] listening"], now=100.0)

    view = build_views(
        [_snap("ft_api", "flight-tracker"), _snap("ft_agent", "flight-tracker")],
        {},
        phase_tracker=tracker,
        now=100.0,
    )[0]

    assert view.phase == "populating_data"
    assert view.phase_label == "Populating data"
    assert view.phase_detail == "global sweep 1/3"
    assert {c.name: c.phase for c in view.containers} == {
        "ft_api": "ready",
        "ft_agent": "populating_data",
    }


# -- demo_ready ----------------------------------------------------------


def test_demo_ready_requires_the_ready_container_to_be_healthy():
    registry = {
        "ft": ProjectConfig(name="ft", compose_file="/x.yml", ready_container="ft_frontend")
    }
    starting = build_views(
        [_snap("ft_frontend", "ft", health="starting"), _snap("ft_api", "ft")], registry
    )[0]
    assert starting.demo_ready is False

    healthy = build_views(
        [_snap("ft_frontend", "ft", health="healthy"), _snap("ft_api", "ft")], registry
    )[0]
    assert healthy.demo_ready is True


def test_demo_ready_is_false_while_the_app_says_it_is_still_starting():
    """Docker health alone lies here: nginx is up and serving a page that
    renders an empty map because the database has nothing in it yet."""
    registry = {
        "ft": ProjectConfig(name="ft", compose_file="/x.yml", ready_container="ft_frontend")
    }
    tracker = PhaseTracker()
    tracker.observe("ft_api", ["[phase:starting_up] loading airports"], now=100.0)

    view = build_views(
        [_snap("ft_frontend", "ft", health="healthy"), _snap("ft_api", "ft")],
        registry,
        phase_tracker=tracker,
        now=100.0,
    )[0]
    assert view.demo_ready is False


def test_demo_ready_is_false_for_a_stopped_project():
    registry = {"ft": ProjectConfig(name="ft", compose_file="/x.yml")}
    assert build_views([], registry)[0].demo_ready is False


def test_demo_ready_without_a_ready_container_requires_everything_running():
    registry = {"ft": ProjectConfig(name="ft", compose_file="/x.yml")}
    partial = build_views([_snap("a", "ft"), _snap("b", "ft", state="exited")], registry)[0]
    assert partial.demo_ready is False
    full = build_views([_snap("a", "ft"), _snap("b", "ft")], registry)[0]
    assert full.demo_ready is True


def test_view_serializes_to_json_safe_primitives():
    view = build_views([_snap("a", "p")], {})[0]
    json.dumps(view.to_dict())  # must not raise

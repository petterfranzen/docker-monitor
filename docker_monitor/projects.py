"""Grouping containers into *projects* — the unit a person actually thinks
in ("start the flight tracker"), as opposed to the unit Docker thinks in
("start flight_tracker_backend_api, then _agent, then _estimator, and the
db before any of them").

Two sources, merged:

1. **Compose's own labels.** Every container `docker compose` creates
   carries `com.docker.compose.project`/`.service`, so grouping running
   containers needs no configuration at all — anything on the host shows
   up, which is the same "watch everything by default" stance the alerting
   side takes.
2. **A registry file** (`PROJECTS_FILE`). Needed because a project that is
   fully down has no containers to read labels off, and because the things
   that make a project *controllable* — which compose file drives it, who's
   allowed to start it, how long a guest's lease lasts, where the demo
   lives — aren't derivable from Docker at all.

A project in the registry but absent from Docker is reported as `stopped`
and can be started. A project in Docker but not the registry is reported
read-only: visible in the API, never controllable. That asymmetry is
deliberate and is the main safety property of this module — being
listed is not the same as being launchable, so an unrelated stack on the
NAS can never be started or stopped through this API.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import phases

logger = logging.getLogger("docker_monitor")

STATE_RUNNING = "running"
STATE_PARTIAL = "partial"
STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_STOPPING = "stopping"

DEFAULT_TTL_MINUTES = 60
DEFAULT_MAX_TTL_MINUTES = 120


@dataclass(frozen=True)
class ProjectConfig:
    """One entry from PROJECTS_FILE. Only `name` is required; everything
    else has a defensive default, so a half-filled entry degrades to
    "visible but not controllable" rather than failing to load."""

    name: str
    display_name: str = ""
    description: str = ""
    compose_file: str = ""
    demo_url: str = ""
    repo_url: str = ""
    # Guests (unauthenticated callers) may start/stop this. Requires a
    # compose_file — without one there's nothing to start.
    guest_controllable: bool = False
    # Never auto-stopped, never leased: something meant to stay up.
    always_on: bool = False
    default_ttl_minutes: int = DEFAULT_TTL_MINUTES
    max_ttl_minutes: int = DEFAULT_MAX_TTL_MINUTES
    # Container whose health decides "the demo is actually usable now".
    # Falls back to "every container running" when unset.
    ready_container: str = ""

    @property
    def controllable(self) -> bool:
        """Can this be started/stopped at all (by anyone, including the
        owner)? No compose file means no."""
        return bool(self.compose_file)

    @property
    def guest_startable(self) -> bool:
        return self.controllable and self.guest_controllable and not self.always_on

    def clamp_ttl(self, requested_minutes: Optional[int]) -> int:
        if not requested_minutes or requested_minutes <= 0:
            return self.default_ttl_minutes
        return min(int(requested_minutes), self.max_ttl_minutes)


def load_registry(path: str) -> dict:
    """Parse PROJECTS_FILE into {name: ProjectConfig}. An unreadable or
    malformed file is logged and treated as empty rather than fatal: the
    alerting half of this service predates project control and must keep
    running regardless of whether anything is configured as controllable.
    """
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        logger.exception("Could not read PROJECTS_FILE at %s — no project is controllable", path)
        return {}

    registry = {}
    for name, entry in (raw or {}).items():
        # JSON has no comments, and this file needs explaining. Keys
        # starting with an underscore are documentation, not projects.
        if name.startswith("_"):
            continue
        if not isinstance(entry, dict):
            logger.warning("Skipping project %r in PROJECTS_FILE: entry is not an object", name)
            continue
        known = {f for f in ProjectConfig.__dataclass_fields__ if f != "name"}
        unknown = set(entry) - known
        if unknown:
            logger.warning(
                "Ignoring unknown key(s) %s for project %r in PROJECTS_FILE",
                ", ".join(sorted(unknown)),
                name,
            )
        config = ProjectConfig(name=name, **{k: v for k, v in entry.items() if k in known})
        if config.guest_controllable and not config.compose_file:
            logger.warning(
                "Project %r is guest_controllable but has no compose_file — "
                "it will be visible but not startable",
                name,
            )
        registry[name] = config
    return registry


@dataclass
class ContainerView:
    """Per-container slice of a project's status, for the API."""

    name: str
    service: str
    state: str
    health: Optional[str]
    phase: Optional[str] = None
    phase_label: Optional[str] = None
    phase_detail: str = ""


@dataclass
class ProjectView:
    name: str
    display_name: str
    description: str
    state: str
    containers: list = field(default_factory=list)
    phase: Optional[str] = None
    phase_label: Optional[str] = None
    phase_detail: str = ""
    phase_updated_at: Optional[float] = None
    demo_url: str = ""
    repo_url: str = ""
    demo_ready: bool = False
    controllable: bool = False
    guest_controllable: bool = False
    always_on: bool = False
    known: bool = False  # present in the registry (vs. discovered on the host)
    default_ttl_minutes: int = DEFAULT_TTL_MINUTES
    max_ttl_minutes: int = DEFAULT_MAX_TTL_MINUTES
    busy: bool = False
    lease: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "state": self.state,
            "phase": self.phase,
            "phase_label": self.phase_label,
            "phase_detail": self.phase_detail,
            "phase_updated_at": self.phase_updated_at,
            "demo_url": self.demo_url,
            "repo_url": self.repo_url,
            "demo_ready": self.demo_ready,
            "controllable": self.controllable,
            "guest_controllable": self.guest_controllable,
            "always_on": self.always_on,
            "known": self.known,
            "default_ttl_minutes": self.default_ttl_minutes,
            "max_ttl_minutes": self.max_ttl_minutes,
            "busy": self.busy,
            "lease": self.lease,
            "containers": [
                {
                    "name": c.name,
                    "service": c.service,
                    "state": c.state,
                    "health": c.health,
                    "phase": c.phase,
                    "phase_label": c.phase_label,
                    "phase_detail": c.phase_detail,
                }
                for c in self.containers
            ],
        }


def group_by_project(snapshots: list) -> dict:
    """{project name: [snapshot]} for containers Compose labelled. Anything
    without the label (a plain `docker run`) isn't part of a project and is
    left out — it's still watched for alerting, just not shown here."""
    grouped: dict = {}
    for snap in snapshots:
        project = snap.compose_project
        if not project:
            continue
        grouped.setdefault(project, []).append(snap)
    return grouped


def _aggregate_state(snapshots: list) -> str:
    if not snapshots:
        return STATE_STOPPED
    running = [s for s in snapshots if s.state == "running"]
    if len(running) == len(snapshots):
        return STATE_RUNNING
    if not running:
        return STATE_STOPPED
    return STATE_PARTIAL


def _is_demo_ready(config: ProjectConfig, snapshots: list, state: str, phase) -> bool:
    """Whether the demo is worth linking to yet.

    Two conditions, because either alone lies. Docker health alone says
    "the web server is up" — true of a flight-tracker whose database is
    still empty, which is a blank map, not a demo. The phase alone says
    "the agent is working" — true before nginx is accepting connections.
    Both must agree.
    """
    if state not in (STATE_RUNNING, STATE_PARTIAL):
        return False

    if config.ready_container:
        ready = next((s for s in snapshots if s.name == config.ready_container), None)
        if ready is None or ready.state != "running":
            return False
        if ready.health not in (None, "healthy"):
            return False
    elif state != STATE_RUNNING:
        return False

    if phase is not None and phase.phase in (
        phases.PHASE_STARTING_UP,
        phases.PHASE_SHUTTING_DOWN,
    ):
        return False
    return True


def build_views(
    snapshots: list,
    registry: dict,
    phase_tracker=None,
    now: float = None,
    busy_projects: dict = None,
    leases: dict = None,
) -> list:
    """The full project list the API serves: every registry entry (running
    or not) plus every Compose project discovered on the host.

    `busy_projects` maps a project name to the state to report while an
    operation is in flight ("starting"/"stopping"); `leases` maps a project
    name to its serialized lease.
    """
    grouped = group_by_project(snapshots)
    busy_projects = busy_projects or {}
    leases = leases or {}
    names = sorted(set(grouped) | set(registry))

    views = []
    for name in names:
        config = registry.get(name) or ProjectConfig(name=name)
        project_snaps = sorted(grouped.get(name, []), key=lambda s: s.name)

        container_views = []
        reports = []
        for snap in project_snaps:
            report = phase_tracker.get(snap.name, now) if phase_tracker else None
            reports.append(report)
            container_views.append(
                ContainerView(
                    name=snap.name,
                    service=snap.compose_service or "",
                    state=snap.state,
                    health=snap.health,
                    phase=report.phase if report else None,
                    phase_label=report.label if report else None,
                    phase_detail=report.detail if report else "",
                )
            )

        state = _aggregate_state(project_snaps)
        busy_state = busy_projects.get(name)
        if busy_state:
            # An operation is in flight; report the direction of travel
            # rather than the half-finished container states underneath it,
            # so the UI doesn't flicker through "partial" on the way up.
            state = busy_state

        phase = phases.aggregate(reports)
        views.append(
            ProjectView(
                name=name,
                display_name=config.display_name or name,
                description=config.description,
                state=state,
                containers=container_views,
                phase=phase.phase if phase else None,
                phase_label=phase.label if phase else None,
                phase_detail=phase.detail if phase else "",
                phase_updated_at=phase.reported_at if phase else None,
                demo_url=config.demo_url,
                repo_url=config.repo_url,
                demo_ready=_is_demo_ready(config, project_snaps, state, phase),
                controllable=config.controllable,
                guest_controllable=config.guest_startable,
                always_on=config.always_on,
                known=name in registry,
                default_ttl_minutes=config.default_ttl_minutes,
                max_ttl_minutes=config.max_ttl_minutes,
                busy=bool(busy_state),
                lease=leases.get(name),
            )
        )
    return views

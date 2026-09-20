"""Talks to the Docker Engine API (via the `docker` SDK, which itself talks
to /var/run/docker.sock — see docker-compose.yml for the read-only mount)
and normalizes what we need out of `docker inspect` into a small snapshot
per container.

Kept separate from rules.py so the alerting logic can be unit-tested
against plain data, with no real Docker daemon involved.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# Compose stamps these on every container it creates; they're what lets
# this group containers into projects without being told about any
# specific stack (see projects.py).
LABEL_PROJECT = "com.docker.compose.project"
LABEL_SERVICE = "com.docker.compose.service"


@dataclass(frozen=True)
class ContainerSnapshot:
    id: str
    name: str
    image: str
    state: str  # running / exited / restarting / paused / created / dead
    exit_code: Optional[int]
    health: Optional[str]  # healthy / unhealthy / starting / None (no healthcheck)
    restart_policy: str  # always / unless-stopped / on-failure / "" (none)
    restart_count: int
    labels: dict
    # Epoch seconds the container last entered "running", or None if it
    # never has / Docker reports the zero value. Used to decide whether a
    # container is fresh enough that reading its log history from the start
    # is worthwhile — see logwatch.py's phase backfill.
    started_at: Optional[float] = None

    @property
    def compose_project(self) -> Optional[str]:
        return self.labels.get(LABEL_PROJECT)

    @property
    def compose_service(self) -> Optional[str]:
        return self.labels.get(LABEL_SERVICE)


def _label_matches(labels: dict, spec: str) -> bool:
    if "=" in spec:
        key, _, value = spec.partition("=")
        return labels.get(key.strip()) == value.strip()
    return spec.strip() in labels


def is_watched(snapshot: ContainerSnapshot, cfg) -> bool:
    """Include/exclude filtering. Exclude always wins over include. An
    empty include list means "watch everything" (the default)."""
    name = snapshot.name

    if any(fnmatch.fnmatch(name, pat) for pat in cfg.container_exclude):
        return False
    if any(_label_matches(snapshot.labels, spec) for spec in cfg.label_exclude):
        return False

    if cfg.container_include and not any(
        fnmatch.fnmatch(name, pat) for pat in cfg.container_include
    ):
        return False
    if cfg.label_include and not any(
        _label_matches(snapshot.labels, spec) for spec in cfg.label_include
    ):
        return False

    return True


def _parse_started_at(value) -> Optional[float]:
    """Docker reports StartedAt as RFC3339Nano, and as the zero time
    ("0001-01-01T00:00:00Z") for a container that has never run. Python's
    fromisoformat can't take more than 6 fractional digits, so trim."""
    if not value or value.startswith("0001-01-01"):
        return None
    text = value.replace("Z", "+00:00")
    match = re.match(r"(.*\.\d{1,6})\d*(\+\d{2}:\d{2})$", text)
    if match:
        text = match.group(1) + match.group(2)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def snapshot_from_container(container) -> ContainerSnapshot:
    attrs = container.attrs
    state = attrs.get("State", {}) or {}
    host_config = attrs.get("HostConfig", {}) or {}
    config = attrs.get("Config", {}) or {}
    health = (state.get("Health") or {}).get("Status")  # absent -> no healthcheck
    return ContainerSnapshot(
        id=container.id,
        name=container.name,
        image=config.get("Image", "?"),
        state=state.get("Status", "unknown"),
        exit_code=state.get("ExitCode"),
        health=health,
        restart_policy=(host_config.get("RestartPolicy") or {}).get("Name", ""),
        restart_count=attrs.get("RestartCount", 0),
        labels=config.get("Labels") or {},
        started_at=_parse_started_at(state.get("StartedAt")),
    )


def fetch_snapshots(client) -> list:
    """Every container on the host, running or not (all=True) — we need to
    see exited containers too, that's the whole point of this tool."""
    return [snapshot_from_container(c) for c in client.containers.list(all=True)]

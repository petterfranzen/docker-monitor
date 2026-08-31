"""Talks to the Docker Engine API (via the `docker` SDK, which itself talks
to /var/run/docker.sock — see docker-compose.yml for the read-only mount)
and normalizes what we need out of `docker inspect` into a small snapshot
per container.

Kept separate from rules.py so the alerting logic can be unit-tested
against plain data, with no real Docker daemon involved.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Optional


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
    )


def fetch_snapshots(client) -> list:
    """Every container on the host, running or not (all=True) — we need to
    see exited containers too, that's the whole point of this tool."""
    return [snapshot_from_container(c) for c in client.containers.list(all=True)]

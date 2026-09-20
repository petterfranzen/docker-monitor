"""The thing the poll loop and the HTTP API both talk to.

Previously `main.py` was the whole service: one loop, one notifier, no
callers. Now there are two consumers of the same Docker state — the
alerting engine (unchanged in behaviour) and the control API — so the
shared work of "look at the host, work out what's true" lives here, and
both sides read from it.

One poll does everything: one `containers.list()`, one log fetch per
running container, feeding alerting, phase tracking and the project view
alike. Adding the API deliberately did not add a second polling cadence
against the daemon.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Optional

import docker

from . import leases as leases_mod
from . import projects as projects_mod
from .dockerstate import is_watched, snapshot_from_container
from .lifecycle import (
    OP_RESTART,
    OP_START,
    OP_STOP,
    OP_UPDATE,
    LifecycleManager,
    ProjectBusy,
)
from .logwatch import LogWatcher
from .notifier import build_notifier
from .phases import PhaseTracker, render_project_phase
from .ratelimit import GuestRateLimiter
from .rules import AlertEngine

logger = logging.getLogger("docker_monitor")


class ControlError(Exception):
    """A control request that can't be honoured. `status` is the HTTP code
    the API should return for it."""

    def __init__(self, message: str, status: int = 400, retry_after: int = 0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _own_container_id(client) -> str:
    """Best-effort id of the container this process itself is running in
    (Docker sets the hostname to the container's short id by default).
    Returns "" outside a container (e.g. local `python -m` dev runs) or if
    lookup fails for any reason — callers treat that as "no self to skip."
    """
    try:
        return client.containers.get(socket.gethostname()).id
    except Exception:
        return ""


class MonitorService:
    def __init__(self, cfg, client=None):
        self._cfg = cfg
        self._client = client or docker.from_env()
        self._engine = AlertEngine(cfg)
        self._notifier = build_notifier(cfg)
        self._phases = PhaseTracker(cfg.phase_stale_seconds)
        self._watcher = (
            LogWatcher(
                cfg,
                phase_tracker=self._phases if cfg.phase_tracking_enabled else None,
                alerts_enabled=cfg.log_monitoring_enabled,
            )
            if (cfg.log_monitoring_enabled or cfg.phase_tracking_enabled)
            else None
        )
        self._registry = projects_mod.load_registry(cfg.projects_file)
        self._leases = leases_mod.LeaseStore(cfg.leases_file)
        self._lifecycle = LifecycleManager(cfg, on_change=self._notify_change)
        self._limiter = GuestRateLimiter(cfg)

        # Found live, during this project's own testing: docker-monitor's
        # own log output necessarily echoes back the names/alert-type
        # labels of whatever it's alerting on. If it also log-content-
        # watches *itself*, that printed line can turn around and match one
        # of its own patterns, firing an alert about itself that quotes
        # itself. State-watching itself has no such feedback risk, so only
        # log-content watching skips this process's own container. The same
        # reasoning is why phases are rendered, never re-emitted as markers
        # — see phases.render_phase().
        self._own_container_id = _own_container_id(self._client)

        self._views: list = []
        self._views_lock = threading.Lock()
        self._subscribers: list = []
        self._reconciled = False

    # -- state -----------------------------------------------------------

    @property
    def registry(self) -> dict:
        return self._registry

    def views(self) -> list:
        with self._views_lock:
            return list(self._views)

    def view(self, name: str) -> Optional[projects_mod.ProjectView]:
        return next((v for v in self.views() if v.name == name), None)

    def snapshot_payload(self, now: float = None) -> dict:
        now = time.time() if now is None else now
        views = self.views()
        return {
            "generated_at": now,
            "max_concurrent_guest_projects": self._cfg.max_concurrent_guest_projects,
            "active_guest_projects": self._leases.active_count(now),
            "projects": [view.to_dict() for view in views],
        }

    # -- change notification (SSE) ---------------------------------------

    def subscribe(self, callback) -> None:
        with self._views_lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback) -> None:
        with self._views_lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def _notify_change(self) -> None:
        with self._views_lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback()
            except Exception:  # pragma: no cover - a bad subscriber must not
                logger.exception("Subscriber callback failed")  # break the poll loop

    # -- the poll ---------------------------------------------------------

    def poll_once(self, now: float = None) -> None:
        now = time.time() if now is None else now
        containers = self._client.containers.list(all=True)
        pairs = [(c, snapshot_from_container(c)) for c in containers]
        watched_pairs = [(c, s) for c, s in pairs if is_watched(s, self._cfg)]
        watched_snapshots = [s for _, s in watched_pairs]

        events = self._engine.evaluate(watched_snapshots)

        if self._watcher is not None:
            running_pairs = [
                (c, s)
                for c, s in watched_pairs
                if s.state == "running" and c.id != self._own_container_id
            ]
            events += self._watcher.evaluate(running_pairs, now)

        for event in events:
            if event.recovered and not self._cfg.alert_on_recovery:
                logger.info(
                    "Recovered (not notified, ALERT_ON_RECOVERY=false): %s (%s)",
                    event.container_name,
                    event.alert_type,
                )
                continue
            self._notifier.send(event)

        self._phases.retain(s.name for s in watched_snapshots)
        previous = self._rebuild_views(watched_snapshots, now)

        if not self._reconciled:
            self._reconciled = True
            leases_mod.reconcile(
                self._leases, self.views(), self._cfg.default_ttl_minutes, now
            )
            self._rebuild_views(watched_snapshots, now)

        self._enforce_leases(now)
        self._log_phase_changes(previous)

        if self._changed(previous):
            self._notify_change()

    def _rebuild_views(self, snapshots: list, now: float) -> list:
        views = projects_mod.build_views(
            snapshots,
            self._registry,
            phase_tracker=self._phases,
            now=now,
            busy_projects=self._lifecycle.busy_states(),
            leases=self._leases.as_dicts(now),
        )
        with self._views_lock:
            previous = self._views
            self._views = views
        return previous

    def _changed(self, previous: list) -> bool:
        return _fingerprint(previous) != _fingerprint(self.views())

    def _log_phase_changes(self, previous: list) -> None:
        """Surface phase transitions in docker-monitor's own logs — useful
        when debugging why a demo never went demo_ready. Rendered via
        render_phase(), never as a raw marker."""
        before = {v.name: v.phase for v in previous}
        for view in self.views():
            if view.phase and before.get(view.name) != view.phase:
                logger.info("%s", render_project_phase(view.name, view.phase, view.phase_detail))

    def _enforce_leases(self, now: float) -> None:
        """Stop anything whose time is up, and drop leases that have
        outlived what they were holding.

        The first is the backstop the whole guest-start feature rests on:
        without it a visitor's demo runs until someone notices.

        The second matters just as much and is less obvious. A lease
        counts against MAX_CONCURRENT_GUEST_PROJECTS whether or not the
        project it names is actually running — so a stack that crashed, or
        a `compose up` that failed, would hold the only demo slot for the
        rest of its TTL while nothing ran at all, and every later visitor
        would be told someone else was using it. Found by the portfolio's
        own journey test, which started a demo, was interrupted, and then
        could not start anything again.

        Releasing is safe here because a project mid-start reports
        `starting`, not `stopped` (see build_views), and the busy check
        below covers the window before the first poll sees its containers.
        """
        views = {v.name: v for v in self.views()}

        for lease in list(self._leases.all()):
            view = views.get(lease.project)
            gone = view is None or (
                view.state == projects_mod.STATE_STOPPED
                and not self._lifecycle.is_busy(lease.project)
            )
            if gone:
                logger.info(
                    "Releasing lease for %s: it is not running (stopped by hand, "
                    "failed to start, or crashed) — holding the lease would block "
                    "the demo slot for nothing",
                    lease.project,
                )
                self._leases.release(lease.project)

        for lease in self._leases.expired(now):
            view = views.get(lease.project)
            if view is None or view.state == projects_mod.STATE_STOPPED:
                self._leases.release(lease.project)
                continue
            if self._lifecycle.is_busy(lease.project):
                continue
            config = self._registry.get(lease.project)
            if config is None or not config.controllable:
                logger.warning(
                    "Lease for %s expired but it is no longer controllable — "
                    "releasing the lease without stopping anything",
                    lease.project,
                )
                self._leases.release(lease.project)
                continue
            logger.info(
                "Lease for %s expired after %d minutes — stopping it",
                lease.project,
                int((lease.expires_at - lease.started_at) / 60),
            )
            try:
                self._lifecycle.submit(config, OP_STOP)
            except ProjectBusy:
                pass

    def poll_interval(self) -> float:
        """How long to wait before the next poll.

        The alerting side is happy at 30s. A dashboard someone is watching
        a stack boot on is not — so while anything is mid-operation or
        holding a lease, poll faster. Idle hosts (the overwhelming majority
        of the time) keep the original cadence and the original load on the
        daemon.
        """
        if self._lifecycle.busy_states() or self._leases.active_count():
            return self._cfg.active_poll_interval_seconds
        return self._cfg.poll_interval_seconds

    # -- control ----------------------------------------------------------

    def _require_config(self, name: str):
        config = self._registry.get(name)
        if config is None:
            raise ControlError(f"unknown project {name!r}", status=404)
        if not config.controllable:
            raise ControlError(
                f"{name} is visible but not controllable (no compose_file configured)",
                status=403,
            )
        return config

    def start_project(self, name: str, ttl_minutes=None, owner: bool = False, ip: str = "") -> dict:
        config = self._require_config(name)

        if not owner:
            if not config.guest_startable:
                raise ControlError(f"{name} cannot be started by guests", status=403)
            decision = self._limiter.check(ip)
            if not decision.allowed:
                raise ControlError(decision.reason, status=429, retry_after=decision.retry_after_seconds)

            # The concurrency cap. Counted over leases rather than running
            # projects so an owner-started, un-leased stack doesn't lock
            # guests out of the one slot.
            active = self._leases.active_count()
            if self._leases.get(name) is None and active >= self._cfg.max_concurrent_guest_projects:
                raise ControlError(
                    "another demo is already running — try again when it finishes",
                    status=409,
                )

        ttl = config.clamp_ttl(ttl_minutes)
        view = self.view(name)

        if view is not None and view.state == projects_mod.STATE_RUNNING and not self._lifecycle.is_busy(name):
            # Already up: don't churn the containers, just (re)set the
            # clock so the caller gets a full window.
            lease = (
                self._leases.extend(name, ttl)
                if self._leases.get(name)
                else self._leases.grant(name, ttl, started_by=_owner_label(owner))
            )
            self._notify_change()
            return {"started": False, "already_running": True, "lease": lease.to_dict()}

        try:
            self._lifecycle.submit(config, OP_START)
        except ProjectBusy as exc:
            raise ControlError(str(exc), status=409) from exc

        lease = None
        if not config.always_on:
            lease = self._leases.grant(name, ttl, started_by=_owner_label(owner))
        return {
            "started": True,
            "already_running": False,
            "lease": lease.to_dict() if lease else None,
        }

    def stop_project(self, name: str, owner: bool = False, ip: str = "") -> dict:
        config = self._require_config(name)

        if not owner:
            if not config.guest_startable:
                raise ControlError(f"{name} cannot be stopped by guests", status=403)
            decision = self._limiter.check(ip)
            if not decision.allowed:
                raise ControlError(decision.reason, status=429, retry_after=decision.retry_after_seconds)

        try:
            self._lifecycle.submit(config, OP_STOP)
        except ProjectBusy as exc:
            raise ControlError(str(exc), status=409) from exc
        self._leases.release(name)
        return {"stopped": True}

    def restart_project(self, name: str) -> dict:
        config = self._require_config(name)
        try:
            self._lifecycle.submit(config, OP_RESTART)
        except ProjectBusy as exc:
            raise ControlError(str(exc), status=409) from exc
        return {"restarted": True}

    def update_project(self, name: str) -> dict:
        config = self._require_config(name)
        try:
            self._lifecycle.submit(config, OP_UPDATE)
        except ProjectBusy as exc:
            raise ControlError(str(exc), status=409) from exc
        return {"updating": True}

    def last_result(self, name: str):
        result = self._lifecycle.last_result(name)
        return result.to_dict() if result else None

    def shutdown(self) -> None:
        self._lifecycle.shutdown()


def _owner_label(owner: bool) -> str:
    return "owner" if owner else leases_mod.OWNER_GUEST


def _fingerprint(views: list) -> tuple:
    """What counts as "something changed" for the purposes of pushing an
    SSE update. Deliberately excludes the lease countdown — that ticks
    every second and the client can count down on its own, so including it
    would turn the event stream into a per-second firehose."""
    return tuple(
        (
            v.name,
            v.state,
            v.phase,
            v.phase_detail,
            v.demo_ready,
            v.busy,
            bool(v.lease),
            tuple((c.name, c.state, c.health, c.phase) for c in v.containers),
        )
        for v in views
    )

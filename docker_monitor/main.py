"""Entry point: poll Docker on an interval, run the container-state alert
engine and (optionally) the log-content watcher, dispatch notifications.

Run directly with `python -m docker_monitor.main` (dev machine, needs the
`docker` package installed and access to /var/run/docker.sock), or via
`docker compose up` using this repo's Dockerfile/docker-compose.yml, which
is how it's meant to run for real on a NAS.
"""
from __future__ import annotations

import logging
import socket
import time

import docker

from .config import Config
from .dockerstate import is_watched, snapshot_from_container
from .logwatch import LogWatcher
from .notifier import build_notifier
from .rules import AlertEngine


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


def run() -> None:
    cfg = Config.load()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("docker_monitor")
    logger.info(
        "Starting docker-monitor: notify_mode=%s poll_interval=%ss "
        "restart_loop_threshold=%s restart_loop_window=%ss log_monitoring=%s",
        cfg.notify_mode,
        cfg.poll_interval_seconds,
        cfg.restart_loop_threshold,
        cfg.restart_loop_window_seconds,
        cfg.log_monitoring_enabled,
    )
    if cfg.notify_mode == "console":
        logger.info(
            "NOTIFY_MODE=console: alerts are logged here, not pushed. "
            "Set NOTIFY_MODE=ntfy (and NTFY_URL/NTFY_TOPIC) once you have a real ntfy server/topic."
        )

    client = docker.from_env()
    engine = AlertEngine(cfg)
    log_watcher = LogWatcher(cfg) if cfg.log_monitoring_enabled else None
    notifier = build_notifier(cfg)

    # Found live, during this project's own testing: docker-monitor's own
    # log output necessarily echoes back the names/alert-type labels of
    # whatever it's alerting on (e.g. "PROBLEM: some-container
    # (log:rate_limited)"). If it also log-content-watches *itself*, that
    # printed line can turn around and match one of its own patterns,
    # firing an alert about itself that quotes itself — observed for real
    # when a test container's own name happened to contain "ratelimit".
    # State-watching itself has no such feedback risk (Docker's own
    # running/exited/health status carries no alert-describing text), so
    # only log-content watching skips this process's own container.
    own_container_id = _own_container_id(client)

    while True:
        try:
            containers = client.containers.list(all=True)
            pairs = [(c, snapshot_from_container(c)) for c in containers]
            watched_pairs = [(c, s) for c, s in pairs if is_watched(s, cfg)]
            watched_snapshots = [s for _, s in watched_pairs]

            events = engine.evaluate(watched_snapshots)

            if log_watcher is not None:
                running_pairs = [
                    (c, s)
                    for c, s in watched_pairs
                    if s.state == "running" and c.id != own_container_id
                ]
                events += log_watcher.evaluate(running_pairs)

            for event in events:
                if event.recovered and not cfg.alert_on_recovery:
                    logger.info(
                        "Recovered (not notified, ALERT_ON_RECOVERY=false): %s (%s)",
                        event.container_name,
                        event.alert_type,
                    )
                    continue
                notifier.send(event)

            if not events:
                logger.debug(
                    "Poll complete: %d container(s) watched, no state changes",
                    len(watched_snapshots),
                )
        except Exception:
            logger.exception("Error during poll cycle (will retry next interval)")

        time.sleep(cfg.poll_interval_seconds)


if __name__ == "__main__":
    run()

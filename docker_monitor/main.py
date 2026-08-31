"""Entry point: poll Docker on an interval, run the container-state alert
engine and (optionally) the log-content watcher, dispatch notifications.

Run directly with `python -m docker_monitor.main` (dev machine, needs the
`docker` package installed and access to /var/run/docker.sock), or via
`docker compose up` using this repo's Dockerfile/docker-compose.yml, which
is how it's meant to run for real on a NAS.
"""
from __future__ import annotations

import logging
import time

import docker

from .config import Config
from .dockerstate import is_watched, snapshot_from_container
from .logwatch import LogWatcher
from .notifier import build_notifier
from .rules import AlertEngine


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

    while True:
        try:
            containers = client.containers.list(all=True)
            pairs = [(c, snapshot_from_container(c)) for c in containers]
            watched_pairs = [(c, s) for c, s in pairs if is_watched(s, cfg)]
            watched_snapshots = [s for _, s in watched_pairs]

            events = engine.evaluate(watched_snapshots)

            if log_watcher is not None:
                running_pairs = [(c, s) for c, s in watched_pairs if s.state == "running"]
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

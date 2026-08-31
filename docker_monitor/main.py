"""Entry point: poll Docker on an interval, run the alert engine, dispatch
notifications.

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
from .dockerstate import fetch_snapshots, is_watched
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
        "restart_loop_threshold=%s restart_loop_window=%ss",
        cfg.notify_mode,
        cfg.poll_interval_seconds,
        cfg.restart_loop_threshold,
        cfg.restart_loop_window_seconds,
    )
    if cfg.notify_mode == "console":
        logger.info(
            "NOTIFY_MODE=console: alerts are logged here, not emailed. "
            "Set NOTIFY_MODE=email (and SMTP_*) once you have real credentials."
        )

    client = docker.from_env()
    engine = AlertEngine(cfg)
    notifier = build_notifier(cfg)

    while True:
        try:
            snapshots = [s for s in fetch_snapshots(client) if is_watched(s, cfg)]
            events = engine.evaluate(snapshots)
            for event in events:
                if event.recovered and not cfg.alert_on_recovery:
                    logger.info(
                        "Recovered (not emailed, ALERT_ON_RECOVERY=false): %s (%s)",
                        event.container_name,
                        event.alert_type,
                    )
                    continue
                notifier.send(event)
            if not events:
                logger.debug(
                    "Poll complete: %d container(s) watched, no state changes",
                    len(snapshots),
                )
        except Exception:
            logger.exception("Error during poll cycle (will retry next interval)")

        time.sleep(cfg.poll_interval_seconds)


if __name__ == "__main__":
    run()

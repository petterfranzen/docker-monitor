"""Entry point.

Two shapes, chosen by `API_ENABLED`:

- **With the API** (the default): uvicorn serves the control API and the
  poll loop runs beside it as an asyncio task in the same process. One
  container, one Docker connection, one poll cadence.
- **Without it** (`API_ENABLED=false`): the original plain
  `while True: poll; sleep` loop and nothing listening. This is not a
  vestigial path — it's the right shape for an instance that only alerts,
  and it means the alerting half never depends on the web stack being
  importable or a port being free.

Run directly with `python -m docker_monitor.main` (dev machine, needs the
`docker` package installed and access to the Docker socket), or via
`docker compose up`, which is how it's meant to run for real on a NAS.
"""
from __future__ import annotations

import logging
import time

from .config import Config
from .service import MonitorService


def _configure_logging(cfg) -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("docker_monitor")
    logger.info(
        "Starting docker-monitor: notify_mode=%s poll_interval=%ss "
        "restart_loop_threshold=%s restart_loop_window=%ss log_monitoring=%s "
        "phase_tracking=%s api=%s",
        cfg.notify_mode,
        cfg.poll_interval_seconds,
        cfg.restart_loop_threshold,
        cfg.restart_loop_window_seconds,
        cfg.log_monitoring_enabled,
        cfg.phase_tracking_enabled,
        f"{cfg.api_host}:{cfg.api_port}" if cfg.api_enabled else "disabled",
    )
    if cfg.notify_mode == "console":
        logger.info(
            "NOTIFY_MODE=console: alerts are logged here, not pushed. "
            "Set NOTIFY_MODE=ntfy (and NTFY_URL/NTFY_TOPIC) once you have a real ntfy server/topic."
        )
    return logger


def _run_loop_only(service: MonitorService, cfg, logger: logging.Logger) -> None:
    while True:
        try:
            service.poll_once()
        except Exception:
            logger.exception("Error during poll cycle (will retry next interval)")
        time.sleep(service.poll_interval())


def _run_with_api(service: MonitorService, cfg, logger: logging.Logger) -> None:
    import uvicorn

    from .api import create_app

    app = create_app(service, cfg)

    if not service.registry:
        logger.warning(
            "API is enabled but no projects are configured (PROJECTS_FILE is unset "
            "or empty): projects discovered on the host will be listed read-only, "
            "and nothing can be started or stopped."
        )

    uvicorn.run(
        app,
        host=cfg.api_host,
        port=cfg.api_port,
        log_level=cfg.log_level.lower(),
        # The host's own reverse proxy sets X-Forwarded-For; whether we
        # believe it is our decision, made in ratelimit.resolve_client_ip
        # against TRUST_PROXY_HEADERS, not uvicorn's.
        proxy_headers=False,
        access_log=False,
    )


def run() -> None:
    cfg = Config.load()
    logger = _configure_logging(cfg)
    service = MonitorService(cfg)

    if cfg.api_enabled:
        _run_with_api(service, cfg, logger)
    else:
        _run_loop_only(service, cfg, logger)


if __name__ == "__main__":
    run()

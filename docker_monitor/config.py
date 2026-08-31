"""Configuration loading: environment variables, with an optional .env file
for local (non-docker-compose) runs.

Docker Compose already loads .env itself via `env_file:` in
docker-compose.yml, so this loader only matters when running
`python -m docker_monitor.main` directly on a dev machine.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader — real environment variables always win over the
    file. Deliberately not a dependency (python-dotenv): this is ~10 lines,
    and this project otherwise only needs the `docker` package."""
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _split_csv(value: str) -> list:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool(value: str, default: bool) -> bool:
    if not value:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    notify_mode: str
    poll_interval_seconds: int
    restart_loop_threshold: int
    restart_loop_window_seconds: int
    container_include: list
    container_exclude: list
    label_include: list
    label_exclude: list
    alert_on_recovery: bool
    log_level: str

    # ntfy (https://ntfy.sh) push notifications.
    ntfy_url: str
    ntfy_topic: str

    # Log-content monitoring (see docker_monitor/logwatch.py).
    log_monitoring_enabled: bool
    no_data_idle_seconds: int
    no_data_exclude: list
    log_pattern_rate_limited_enabled: bool
    log_pattern_generic_error_enabled: bool
    log_patterns_file: str
    traffic_baseline_window_seconds: int
    traffic_spike_multiplier: float
    traffic_min_baseline_samples: int
    traffic_grace_period_seconds: int
    traffic_min_rate_lines_per_min: float

    @staticmethod
    def load() -> "Config":
        _load_dotenv()
        env = os.environ

        notify_mode = env.get("NOTIFY_MODE", "console").strip().lower()
        if notify_mode not in ("console", "ntfy"):
            raise ValueError(
                f"NOTIFY_MODE must be 'console' or 'ntfy', got {notify_mode!r}"
            )

        cfg = Config(
            notify_mode=notify_mode,
            poll_interval_seconds=int(env.get("POLL_INTERVAL_SECONDS", "30")),
            restart_loop_threshold=int(env.get("RESTART_LOOP_THRESHOLD", "3")),
            restart_loop_window_seconds=int(
                env.get("RESTART_LOOP_WINDOW_SECONDS", "300")
            ),
            container_include=_split_csv(env.get("CONTAINER_INCLUDE", "")),
            container_exclude=_split_csv(env.get("CONTAINER_EXCLUDE", "")),
            label_include=_split_csv(env.get("LABEL_INCLUDE", "")),
            label_exclude=_split_csv(env.get("LABEL_EXCLUDE", "")),
            alert_on_recovery=_bool(env.get("ALERT_ON_RECOVERY", ""), True),
            log_level=env.get("LOG_LEVEL", "INFO").strip().upper(),
            ntfy_url=env.get("NTFY_URL", "http://ntfy:80").strip(),
            ntfy_topic=env.get("NTFY_TOPIC", "").strip(),
            log_monitoring_enabled=_bool(env.get("LOG_MONITORING_ENABLED", ""), True),
            no_data_idle_seconds=int(env.get("NO_DATA_IDLE_SECONDS", "600")),
            no_data_exclude=_split_csv(env.get("NO_DATA_EXCLUDE", "")),
            log_pattern_rate_limited_enabled=_bool(
                env.get("LOG_PATTERN_RATE_LIMITED_ENABLED", ""), True
            ),
            log_pattern_generic_error_enabled=_bool(
                env.get("LOG_PATTERN_GENERIC_ERROR_ENABLED", ""), False
            ),
            log_patterns_file=env.get("LOG_PATTERNS_FILE", "").strip(),
            traffic_baseline_window_seconds=int(
                env.get("TRAFFIC_BASELINE_WINDOW_SECONDS", "1800")
            ),
            traffic_spike_multiplier=float(env.get("TRAFFIC_SPIKE_MULTIPLIER", "5")),
            traffic_min_baseline_samples=int(
                env.get("TRAFFIC_MIN_BASELINE_SAMPLES", "5")
            ),
            traffic_grace_period_seconds=int(
                env.get("TRAFFIC_GRACE_PERIOD_SECONDS", "300")
            ),
            traffic_min_rate_lines_per_min=float(
                env.get("TRAFFIC_MIN_RATE_LINES_PER_MIN", "2")
            ),
        )

        if cfg.notify_mode == "ntfy" and not cfg.ntfy_topic:
            raise ValueError("NOTIFY_MODE=ntfy requires NTFY_TOPIC")
        return cfg

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


def _split_csv(value: str) -> list[str]:
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

    smtp_host: str
    smtp_port: int
    smtp_tls_mode: str
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_to: list

    @staticmethod
    def load() -> "Config":
        _load_dotenv()
        env = os.environ

        notify_mode = env.get("NOTIFY_MODE", "console").strip().lower()
        if notify_mode not in ("console", "email"):
            raise ValueError(
                f"NOTIFY_MODE must be 'console' or 'email', got {notify_mode!r}"
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
            smtp_host=env.get("SMTP_HOST", "").strip(),
            smtp_port=int(env.get("SMTP_PORT", "587")),
            smtp_tls_mode=env.get("SMTP_TLS_MODE", "starttls").strip().lower(),
            smtp_username=env.get("SMTP_USERNAME", "").strip(),
            smtp_password=env.get("SMTP_PASSWORD", ""),
            smtp_from=env.get("SMTP_FROM", "").strip(),
            smtp_to=_split_csv(env.get("SMTP_TO", "")),
        )

        if cfg.notify_mode == "email":
            missing = [
                name
                for name, value in (
                    ("SMTP_HOST", cfg.smtp_host),
                    ("SMTP_FROM", cfg.smtp_from),
                )
                if not value
            ]
            if not cfg.smtp_to:
                missing.append("SMTP_TO")
            if missing:
                raise ValueError(
                    "NOTIFY_MODE=email requires " + ", ".join(missing)
                )
        return cfg

"""Starting, stopping, restarting and updating a project, by driving the
`docker compose` CLI against the socket proxy.

**Why the CLI and not the Engine API**, when the rest of this service is
careful to only ever use docker-py: because "update" honestly means "pull
a newer image and recreate the container with the same configuration," and
doing that through the raw API means re-deriving a container's full create
spec from its inspect output — networks, aliases, mounts, env, every
Compose label — and getting it exactly right, Watchtower-style. Compose
already does this correctly, the compose files are already on the NAS in
each repo's `deploy/`, and `docker compose pull && up -d` is literally the
documented update procedure in flight-tracker's own deploy README. Using
the same command a human would use keeps the two from drifting apart.

The CLI still never touches the host socket directly: `DOCKER_HOST` points
at the socket proxy, same as docker-py does.

Everything here runs off the request thread. `docker compose up -d` on a
four-service stack takes tens of seconds; an HTTP handler that waits for
it would hold the connection open long past any sensible proxy timeout.
Operations are submitted to a small thread pool, the project is marked
busy for the duration, and the caller polls (or watches the SSE stream)
for the result.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from .projects import STATE_STARTING, STATE_STOPPING

logger = logging.getLogger("docker_monitor")

OP_START = "start"
OP_STOP = "stop"
OP_RESTART = "restart"
OP_UPDATE = "update"

# Which state a project reports while each operation is in flight.
_OP_STATE = {
    OP_START: STATE_STARTING,
    OP_STOP: STATE_STOPPING,
    OP_RESTART: STATE_STARTING,
    OP_UPDATE: STATE_STARTING,
}

# Per-operation wall-clock ceilings. `up` can legitimately take a while on
# a cold stack (image pulls, Postgres initdb, Spring Boot start); `stop`
# should not, since Compose sends SIGTERM and then SIGKILLs after its own
# grace period. A timeout kills the subprocess and surfaces as a failed
# operation rather than a wedged "starting" state forever.
_OP_TIMEOUTS = {
    OP_START: 600,
    OP_STOP: 180,
    OP_RESTART: 600,
    OP_UPDATE: 1800,
}


@dataclass
class OperationResult:
    project: str
    operation: str
    ok: bool
    detail: str = ""
    finished_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "operation": self.operation,
            "ok": self.ok,
            "detail": self.detail,
            "finished_at": self.finished_at,
        }


class ProjectBusy(Exception):
    """Another operation is already in flight for this project."""


class LifecycleManager:
    def __init__(self, cfg, on_change=None):
        self._cfg = cfg
        self._on_change = on_change or (lambda: None)
        self._pool = ThreadPoolExecutor(
            max_workers=cfg.max_concurrent_operations, thread_name_prefix="lifecycle"
        )
        self._lock = threading.Lock()
        self._in_flight: dict = {}  # project -> operation
        self._last_results: dict = {}  # project -> OperationResult

    # -- introspection ---------------------------------------------------

    def busy_states(self) -> dict:
        """{project: state-to-report} for everything currently mid-operation."""
        with self._lock:
            return {
                project: _OP_STATE.get(operation, STATE_STARTING)
                for project, operation in self._in_flight.items()
            }

    def is_busy(self, project: str) -> bool:
        with self._lock:
            return project in self._in_flight

    def last_result(self, project: str) -> Optional[OperationResult]:
        with self._lock:
            return self._last_results.get(project)

    # -- running operations ----------------------------------------------

    def submit(self, config, operation: str) -> None:
        """Queue an operation. Raises ProjectBusy if one is already running
        for this project, so two rapid clicks can't race `up` against
        `stop`."""
        if not config.controllable:
            raise ValueError(f"project {config.name!r} has no compose_file and cannot be controlled")
        with self._lock:
            if config.name in self._in_flight:
                raise ProjectBusy(
                    f"{config.name} is already {self._in_flight[config.name]}ing"
                )
            self._in_flight[config.name] = operation
        self._on_change()
        self._pool.submit(self._run, config, operation)

    def _run(self, config, operation: str) -> None:
        started = time.time()
        try:
            result = self._execute(config, operation)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unexpected failure running %s on %s", operation, config.name)
            result = OperationResult(config.name, operation, ok=False, detail=str(exc))
        finally:
            with self._lock:
                self._in_flight.pop(config.name, None)
                self._last_results[config.name] = result
        logger.info(
            "%s %s in %.1fs: %s",
            operation,
            config.name,
            time.time() - started,
            "ok" if result.ok else f"FAILED — {result.detail}",
        )
        self._on_change()

    def _execute(self, config, operation: str) -> OperationResult:
        commands = self._commands(operation)
        for command in commands:
            completed = self._compose(config, command, _OP_TIMEOUTS.get(operation, 600))
            if completed.returncode != 0:
                return OperationResult(
                    config.name,
                    operation,
                    ok=False,
                    detail=_tail(completed.stderr or completed.stdout),
                )
        return OperationResult(config.name, operation, ok=True)

    @staticmethod
    def _commands(operation: str) -> list:
        if operation == OP_START:
            return [["up", "-d"]]
        if operation == OP_STOP:
            # `stop`, not `down`: `down` removes containers and (with -v)
            # volumes. Keeping the containers and the Postgres volume means
            # the next start is fast and the flight-tracker's accumulated
            # position history survives a demo ending — restarting into an
            # empty database every time would make the "populating data"
            # wait happen on every single visit.
            return [["stop"]]
        if operation == OP_RESTART:
            return [["restart"]]
        if operation == OP_UPDATE:
            # Pull first, then recreate. `up -d` on its own would happily
            # keep running the old image.
            return [["pull"], ["up", "-d"]]
        raise ValueError(f"unknown operation {operation!r}")

    def _compose(self, config, args: list, timeout: int):
        command = [
            "docker",
            "compose",
            "--project-name",
            config.name,
            "--file",
            config.compose_file,
            *args,
        ]
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp",
            # Same endpoint docker-py uses. Never the host socket directly.
            "DOCKER_HOST": self._cfg.docker_host,
        }
        logger.info("Running: %s", " ".join(command))
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                command,
                returncode=124,
                stdout="",
                stderr=f"timed out after {timeout}s",
            )
        except FileNotFoundError:
            return subprocess.CompletedProcess(
                command,
                returncode=127,
                stdout="",
                stderr=(
                    "the `docker compose` CLI is not available in this image — "
                    "project control needs it (see Dockerfile)"
                ),
            )

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def _tail(text: str, limit: int = 600) -> str:
    """Last bit of a failed command's output. Compose failures put the
    useful line at the end (the service that failed, the port that was
    already bound), and the whole thing can be pages of pull progress."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]

"""Unit tests for project lifecycle operations
(docker_monitor/lifecycle.py).

No real `docker compose` runs here: the subprocess call is stubbed, and
what's asserted is the command that *would* have run and how failures and
concurrency are handled.
"""
import subprocess
import threading
from dataclasses import dataclass

import pytest

from docker_monitor.lifecycle import (
    OP_START,
    OP_STOP,
    OP_UPDATE,
    LifecycleManager,
    ProjectBusy,
    _tail,
)
from docker_monitor.projects import STATE_STARTING, STATE_STOPPING, ProjectConfig


@dataclass
class _Cfg:
    max_concurrent_operations: int = 2
    docker_host: str = "tcp://socket-proxy:2375"


CONFIG = ProjectConfig(name="flight-tracker", compose_file="/stacks/ft/docker-compose.yml")


class _Recorder:
    """Stands in for subprocess.run, recording commands instead of running
    them."""

    def __init__(self, returncode=0, stderr=""):
        self.commands = []
        self.envs = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        self.envs.append(kwargs.get("env") or {})
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


def _run_sync(manager, config, operation):
    """Run an operation on this thread so assertions don't race the pool."""
    manager._run(config, operation)


def test_start_runs_compose_up_against_the_proxy(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    manager = LifecycleManager(_Cfg())

    _run_sync(manager, CONFIG, OP_START)

    assert recorder.commands == [
        [
            "docker",
            "compose",
            "--project-name",
            "flight-tracker",
            "--file",
            "/stacks/ft/docker-compose.yml",
            "up",
            "-d",
        ]
    ]
    # Never the host socket directly — same endpoint docker-py uses.
    assert recorder.envs[0]["DOCKER_HOST"] == "tcp://socket-proxy:2375"


def test_stop_uses_stop_not_down(monkeypatch):
    """`down` would remove containers and orphan the Postgres volume's
    contents behind a fresh stack — every demo would then start from an
    empty database and sit in "populating data" again."""
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    manager = LifecycleManager(_Cfg())

    _run_sync(manager, CONFIG, OP_STOP)

    assert recorder.commands[0][-1] == "stop"
    assert "down" not in recorder.commands[0]


def test_update_pulls_before_recreating(monkeypatch):
    """`up -d` alone would happily keep running the old image."""
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    manager = LifecycleManager(_Cfg())

    _run_sync(manager, CONFIG, OP_UPDATE)

    assert recorder.commands[0][-1] == "pull"
    assert recorder.commands[1][-2:] == ["up", "-d"]


def test_a_failed_step_stops_the_sequence(monkeypatch):
    """A failed `pull` must not be followed by an `up` that silently
    restarts the old image and reports success."""
    recorder = _Recorder(returncode=1, stderr="manifest unknown")
    monkeypatch.setattr(subprocess, "run", recorder)
    manager = LifecycleManager(_Cfg())

    _run_sync(manager, CONFIG, OP_UPDATE)

    assert len(recorder.commands) == 1
    result = manager.last_result("flight-tracker")
    assert result.ok is False
    assert "manifest unknown" in result.detail


def test_missing_compose_cli_is_reported_not_raised(monkeypatch):
    def _raise(*_, **__):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", _raise)
    manager = LifecycleManager(_Cfg())

    _run_sync(manager, CONFIG, OP_START)

    result = manager.last_result("flight-tracker")
    assert result.ok is False
    assert "not available in this image" in result.detail


def test_timeout_is_reported_not_left_hanging(monkeypatch):
    def _timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", _timeout)
    manager = LifecycleManager(_Cfg())

    _run_sync(manager, CONFIG, OP_START)

    result = manager.last_result("flight-tracker")
    assert result.ok is False
    assert "timed out" in result.detail


def test_uncontrollable_project_is_refused():
    manager = LifecycleManager(_Cfg())
    with pytest.raises(ValueError, match="compose_file"):
        manager.submit(ProjectConfig(name="p"), OP_START)


def test_second_operation_on_a_busy_project_is_refused(monkeypatch):
    """Two rapid clicks must not race `up` against `stop`."""
    release = threading.Event()

    def _blocking(command, **kwargs):
        release.wait(timeout=5)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _blocking)
    manager = LifecycleManager(_Cfg())
    try:
        manager.submit(CONFIG, OP_START)
        # Wait for the worker thread to actually claim the project.
        for _ in range(500):
            if manager.is_busy("flight-tracker"):
                break
            threading.Event().wait(0.01)

        assert manager.busy_states() == {"flight-tracker": STATE_STARTING}
        with pytest.raises(ProjectBusy):
            manager.submit(CONFIG, OP_STOP)
    finally:
        release.set()
        manager.shutdown()


def test_busy_state_reflects_the_operation(monkeypatch):
    manager = LifecycleManager(_Cfg())
    manager._in_flight["p"] = OP_STOP
    assert manager.busy_states() == {"p": STATE_STOPPING}


def test_tail_keeps_the_end_of_long_output():
    """Compose failures put the useful line last; the rest is pull
    progress."""
    assert _tail("short") == "short"
    trimmed = _tail("x" * 1000 + "the real error", limit=50)
    assert trimmed.startswith("…")
    assert trimmed.endswith("the real error")

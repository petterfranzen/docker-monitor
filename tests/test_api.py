"""End-to-end tests for the control API (docker_monitor/api.py) over a fake
Docker daemon.

These are the tests that matter most in this repo: the API is publicly
reachable by design, so "a guest can only do exactly what they're meant
to" is a property worth asserting directly rather than inferring from the
unit tests of the pieces.

No real Docker and no real `docker compose`: a fake client supplies
container state, and lifecycle operations are intercepted and recorded.
"""
import json

import pytest
from fastapi.testclient import TestClient

from docker_monitor import api
from docker_monitor.api import _sse, create_app
from docker_monitor.config import Config
from docker_monitor.dockerstate import LABEL_PROJECT, LABEL_SERVICE
from docker_monitor.service import MonitorService

TOKEN = "test-owner-token"
OWNER = {"Authorization": f"Bearer {TOKEN}"}
PUBLIC_CLIENT = {"x-forwarded-for": "8.8.8.8"}


# -- fake docker ---------------------------------------------------------


class FakeContainer:
    def __init__(self, name, project=None, service="", state="running", health=None, logs=b""):
        self.id = f"id-{name}"
        self.name = name
        self._logs = logs
        labels = {}
        if project:
            labels[LABEL_PROJECT] = project
            labels[LABEL_SERVICE] = service or name
        state_attrs = {"Status": state, "ExitCode": 0 if state == "exited" else None}
        if health:
            state_attrs["Health"] = {"Status": health}
        self.attrs = {
            "State": state_attrs,
            "HostConfig": {"RestartPolicy": {"Name": ""}},
            "Config": {"Image": "img", "Labels": labels},
            "RestartCount": 0,
        }

    def logs(self, **_):
        return self._logs


class FakeContainers:
    def __init__(self, containers):
        self.containers = containers

    def list(self, all=False):  # noqa: A002 - matches docker-py's signature
        return list(self.containers)

    def get(self, _):
        raise KeyError("no self container in tests")


class FakeClient:
    def __init__(self, containers=()):
        self.containers = FakeContainers(list(containers))


# -- fixtures ------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A registry with one guest-controllable project and one that is
    owner-only, plus an isolated lease file."""
    registry = tmp_path / "projects.json"
    registry.write_text(
        json.dumps(
            {
                "flight-tracker": {
                    "display_name": "Flight Tracker",
                    "compose_file": str(tmp_path / "ft.yml"),
                    "guest_controllable": True,
                    "default_ttl_minutes": 60,
                    "max_ttl_minutes": 120,
                    "ready_container": "ft_frontend",
                },
                "private-stack": {
                    "compose_file": str(tmp_path / "private.yml"),
                    "guest_controllable": False,
                },
            }
        )
    )
    for key, value in {
        "NOTIFY_MODE": "console",
        "PROJECTS_FILE": str(registry),
        "LEASES_FILE": str(tmp_path / "leases.json"),
        "CONTROL_TOKEN": TOKEN,
        "TRUST_PROXY_HEADERS": "true",
        "LOG_MONITORING_ENABLED": "false",
        "GUEST_MAX_REQUESTS_PER_WINDOW": "5",
        "GUEST_MAX_REQUESTS_PER_DAY": "20",
        "MAX_CONCURRENT_GUEST_PROJECTS": "1",
    }.items():
        monkeypatch.setenv(key, value)
    return tmp_path


@pytest.fixture
def make_client(env, monkeypatch):
    """Builds (TestClient, service, operations-log) for a given set of fake
    containers. Lifecycle operations are recorded, never executed."""

    def _build(containers=()):
        cfg = Config.load()
        service = MonitorService(cfg, client=FakeClient(containers))
        operations = []

        def _fake_execute(config, operation):
            from docker_monitor.lifecycle import OperationResult

            operations.append((config.name, operation))
            return OperationResult(config.name, operation, ok=True)

        monkeypatch.setattr(service._lifecycle, "_execute", _fake_execute)
        # poll=False: these tests drive polling explicitly so assertions
        # never race a background task.
        app = create_app(service, cfg, poll=False)
        return TestClient(app), service, operations

    return _build


# -- reading -------------------------------------------------------------


def test_lists_registry_projects_even_when_nothing_is_running(make_client):
    client, service, _ = make_client()
    service.poll_once()

    body = client.get("/api/projects").json()
    names = [p["name"] for p in body["projects"]]
    assert names == ["flight-tracker", "private-stack"]
    assert all(p["state"] == "stopped" for p in body["projects"])


def test_surfaces_container_state_and_phase(make_client):
    containers = [
        FakeContainer("ft_frontend", "flight-tracker", "frontend", health="healthy"),
        FakeContainer("ft_agent", "flight-tracker", "backend-agent"),
    ]
    client, service, _ = make_client(containers)
    service.poll_once()

    project = client.get("/api/projects/flight-tracker").json()
    assert project["state"] == "running"
    assert project["display_name"] == "Flight Tracker"
    assert {c["name"] for c in project["containers"]} == {"ft_frontend", "ft_agent"}


def test_unknown_project_is_a_404(make_client):
    client, service, _ = make_client()
    service.poll_once()
    assert client.get("/api/projects/nope").status_code == 404


def test_healthz(make_client):
    client, _, _ = make_client()
    assert client.get("/healthz").json() == {"ok": True}


# -- guest control -------------------------------------------------------


def test_guest_can_start_a_guest_controllable_project(make_client):
    client, service, operations = make_client()
    service.poll_once()

    response = client.post("/api/projects/flight-tracker/start", headers=PUBLIC_CLIENT)

    assert response.status_code == 200
    assert response.json()["started"] is True
    assert operations == [("flight-tracker", "start")]


def test_starting_grants_a_lease_with_the_default_ttl(make_client):
    client, service, _ = make_client()
    service.poll_once()

    lease = client.post("/api/projects/flight-tracker/start", headers=PUBLIC_CLIENT).json()["lease"]
    assert lease["started_by"] == "guest"
    assert 3500 < lease["seconds_remaining"] <= 3600


def test_requested_ttl_is_clamped_to_the_configured_maximum(make_client):
    """A guest asking for a week gets two hours."""
    client, service, _ = make_client()
    service.poll_once()

    lease = client.post(
        "/api/projects/flight-tracker/start",
        json={"ttl_minutes": 10080},
        headers=PUBLIC_CLIENT,
    ).json()["lease"]
    assert lease["seconds_remaining"] <= 120 * 60


def test_guest_cannot_start_a_project_not_marked_guest_controllable(make_client):
    client, service, operations = make_client()
    service.poll_once()

    response = client.post("/api/projects/private-stack/start", headers=PUBLIC_CLIENT)

    assert response.status_code == 403
    assert operations == []


def test_guest_cannot_restart_or_update(make_client):
    """The two operations that can recreate containers or pull new images
    are owner-only."""
    client, service, operations = make_client()
    service.poll_once()

    assert client.post("/api/projects/flight-tracker/restart", headers=PUBLIC_CLIENT).status_code == 401
    assert client.post("/api/projects/flight-tracker/update", headers=PUBLIC_CLIENT).status_code == 401
    assert operations == []


def test_an_invalid_token_is_refused(make_client):
    client, service, operations = make_client()
    service.poll_once()

    response = client.post(
        "/api/projects/flight-tracker/update", headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 403
    assert operations == []


def test_concurrency_cap_blocks_a_second_guest_demo(make_client):
    """MAX_CONCURRENT_GUEST_PROJECTS=1: one visitor's demo must not be
    joined by another's."""
    client, service, _ = make_client()
    service.poll_once()

    client.post("/api/projects/flight-tracker/start", headers=PUBLIC_CLIENT)
    service._registry["private-stack"] = service._registry["private-stack"].__class__(
        **{**service._registry["private-stack"].__dict__, "guest_controllable": True}
    )

    response = client.post("/api/projects/private-stack/start", headers={"x-forwarded-for": "1.1.1.1"})
    assert response.status_code == 409
    assert "another demo" in response.json()["detail"]


def test_rate_limited_guest_gets_429_with_retry_after(make_client, monkeypatch):
    monkeypatch.setenv("GUEST_MAX_REQUESTS_PER_WINDOW", "1")
    client, service, _ = make_client()
    service.poll_once()

    client.post("/api/projects/flight-tracker/start", headers=PUBLIC_CLIENT)
    response = client.post("/api/projects/flight-tracker/stop", headers=PUBLIC_CLIENT)

    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0


def test_local_callers_skip_the_guest_limits(make_client, monkeypatch):
    monkeypatch.setenv("GUEST_MAX_REQUESTS_PER_WINDOW", "1")
    client, service, _ = make_client()
    service.poll_once()

    for _ in range(3):
        response = client.post(
            "/api/projects/flight-tracker/start", headers={"x-forwarded-for": "192.168.1.5"}
        )
        assert response.status_code == 200


# -- owner control -------------------------------------------------------


def test_owner_can_update(make_client):
    client, service, operations = make_client()
    service.poll_once()

    assert client.post("/api/projects/flight-tracker/update", headers=OWNER).status_code == 200
    assert operations == [("flight-tracker", "update")]


def test_owner_start_of_an_owner_only_project_is_allowed(make_client):
    client, service, operations = make_client()
    service.poll_once()

    assert client.post("/api/projects/private-stack/start", headers=OWNER).status_code == 200
    assert operations == [("private-stack", "start")]


def test_owner_operations_are_disabled_when_no_token_is_configured(make_client, monkeypatch):
    """"No token" must mean "nobody is the owner", never "everybody is"."""
    monkeypatch.setenv("CONTROL_TOKEN", "")
    client, service, operations = make_client()
    service.poll_once()

    response = client.post("/api/projects/flight-tracker/update")
    assert response.status_code == 503
    assert operations == []


# -- project that is already up -------------------------------------------


def test_starting_an_already_running_project_extends_instead_of_churning(make_client):
    containers = [FakeContainer("ft_frontend", "flight-tracker", "frontend", health="healthy")]
    client, service, operations = make_client(containers)
    service.poll_once()

    body = client.post("/api/projects/flight-tracker/start", headers=PUBLIC_CLIENT).json()

    assert body["already_running"] is True
    assert body["started"] is False
    assert operations == []  # no compose command run
    assert body["lease"]["seconds_remaining"] > 0


# -- the TTL backstop -----------------------------------------------------


def test_an_expired_lease_stops_the_project(make_client):
    """The whole guest-start feature rests on this: nobody has to come back
    and press stop."""
    containers = [FakeContainer("ft_frontend", "flight-tracker", "frontend", health="healthy")]
    client, service, operations = make_client(containers)
    service.poll_once()

    client.post(
        "/api/projects/flight-tracker/start", json={"ttl_minutes": 1}, headers=PUBLIC_CLIENT
    )
    operations.clear()

    service.poll_once(now=__import__("time").time() + 120)

    assert operations == [("flight-tracker", "stop")]


def test_a_lease_that_has_not_expired_leaves_the_project_alone(make_client):
    containers = [FakeContainer("ft_frontend", "flight-tracker", "frontend", health="healthy")]
    client, service, operations = make_client(containers)
    service.poll_once()

    client.post("/api/projects/flight-tracker/start", headers=PUBLIC_CLIENT)
    operations.clear()

    service.poll_once()
    assert operations == []


def test_stopping_releases_the_lease(make_client):
    containers = [FakeContainer("ft_frontend", "flight-tracker", "frontend", health="healthy")]
    client, service, _ = make_client(containers)
    service.poll_once()

    client.post("/api/projects/flight-tracker/start", headers=PUBLIC_CLIENT)
    client.post("/api/projects/flight-tracker/stop", headers=PUBLIC_CLIENT)

    assert service._leases.get("flight-tracker") is None


# -- SSE -------------------------------------------------------------------


def test_sse_frame_format():
    """One `event:`/`data:` pair terminated by a blank line — what
    EventSource in the dashboard parses."""
    frame = _sse({"projects": []})
    assert frame.startswith("event: projects\n")
    assert frame.endswith("\n\n")
    body = frame.split("data: ", 1)[1].strip()
    assert json.loads(body) == {"projects": []}


# The live stream itself is verified with `curl -N` against a real server
# (see README's "Verifying the control API"), not here: Starlette's
# TestClient drives the app through a blocking portal, and a
# StreamingResponse that never ends by design has no clean way to be torn
# down from the client side — the test hangs rather than failing. What's
# unit-testable is the frame format above; what isn't is better checked
# against a real socket.

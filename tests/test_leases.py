"""Unit tests for demo leases (docker_monitor/leases.py).

The TTL is the only thing standing between "a visitor clicked start" and
"a stack runs on the NAS until someone notices", so these cover the
failure modes that would silently disable it: state lost across a restart,
a lease for something that isn't running any more, and something running
with no lease at all.
"""
import json

from docker_monitor.leases import OWNER_ADOPTED, OWNER_GUEST, LeaseStore, reconcile
from docker_monitor.projects import ProjectView


def _view(name, state="running", guest=True, always_on=False):
    return ProjectView(
        name=name,
        display_name=name,
        description="",
        state=state,
        guest_controllable=guest,
        always_on=always_on,
    )


def test_grant_and_expiry(tmp_path):
    store = LeaseStore(str(tmp_path / "leases.json"))
    lease = store.grant("flight-tracker", ttl_minutes=60, now=1000.0)

    assert lease.expires_at == 1000.0 + 3600
    assert lease.seconds_remaining(now=1000.0) == 3600
    assert not lease.is_expired(now=4599.0)
    assert lease.is_expired(now=4600.0)


def test_seconds_remaining_never_goes_negative():
    store = LeaseStore("")
    lease = store.grant("p", ttl_minutes=1, now=0.0)
    assert lease.seconds_remaining(now=99999.0) == 0


def test_leases_survive_a_restart(tmp_path):
    """The whole reason these are on disk: a monitor restart must not
    orphan a running demo stack."""
    path = str(tmp_path / "leases.json")
    LeaseStore(path).grant("flight-tracker", ttl_minutes=60, now=1000.0)

    reloaded = LeaseStore(path)
    lease = reloaded.get("flight-tracker")
    assert lease is not None
    assert lease.expires_at == 1000.0 + 3600
    assert lease.started_by == OWNER_GUEST


def test_corrupt_lease_file_does_not_stop_startup(tmp_path):
    """Refusing to boot on a bad lease file would mean nothing is monitored
    at all; losing the leases just means reconciliation re-adopts."""
    path = tmp_path / "leases.json"
    path.write_text("{ this is not json")
    store = LeaseStore(str(path))
    assert store.all() == []


def test_malformed_entry_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "leases.json"
    path.write_text(json.dumps([{"project": "good", "started_by": "guest", "started_at": 1, "expires_at": 2}, {"nope": 1}]))
    store = LeaseStore(str(path))
    assert [lease.project for lease in store.all()] == ["good"]


def test_release_removes_and_persists(tmp_path):
    path = str(tmp_path / "leases.json")
    store = LeaseStore(path)
    store.grant("p", 60, now=0.0)
    store.release("p")
    assert LeaseStore(path).get("p") is None


def test_extend_keeps_the_original_owner_but_resets_the_clock():
    store = LeaseStore("")
    store.grant("p", ttl_minutes=60, started_by="owner", now=0.0)
    extended = store.extend("p", ttl_minutes=30, now=1800.0)
    assert extended.started_by == "owner"
    assert extended.started_at == 0.0
    assert extended.expires_at == 1800.0 + 1800


def test_active_count_ignores_expired_leases():
    store = LeaseStore("")
    store.grant("a", ttl_minutes=60, now=0.0)
    store.grant("b", ttl_minutes=1, now=0.0)
    assert store.active_count(now=0.0) == 2
    assert store.active_count(now=120.0) == 1


def test_reconcile_releases_leases_for_stopped_projects():
    """Someone stopped it by hand. The lease must go, or its expiry later
    "stops" a project a human has since started for their own reasons."""
    store = LeaseStore("")
    store.grant("flight-tracker", 60, now=0.0)
    reconcile(store, [_view("flight-tracker", state="stopped")], 60, now=10.0)
    assert store.get("flight-tracker") is None


def test_reconcile_adopts_running_projects_with_no_lease():
    """The monitor crashed mid-demo and lost the lease file. Without this
    the stack runs forever — exactly what the TTL exists to prevent."""
    store = LeaseStore("")
    adopted = reconcile(store, [_view("flight-tracker", state="running")], 60, now=100.0)

    assert adopted == ["flight-tracker"]
    lease = store.get("flight-tracker")
    assert lease.started_by == OWNER_ADOPTED
    assert lease.expires_at == 100.0 + 3600


def test_reconcile_never_adopts_always_on_projects():
    store = LeaseStore("")
    reconcile(store, [_view("ntfy", state="running", always_on=True)], 60, now=0.0)
    assert store.all() == []


def test_reconcile_never_adopts_projects_guests_cannot_control():
    """An always-up stack the owner runs is not a demo and must not acquire
    a TTL behind their back."""
    store = LeaseStore("")
    reconcile(store, [_view("something-else", state="running", guest=False)], 60, now=0.0)
    assert store.all() == []


def test_reconcile_leaves_an_existing_valid_lease_alone():
    store = LeaseStore("")
    store.grant("flight-tracker", 60, now=0.0)
    reconcile(store, [_view("flight-tracker", state="running")], 60, now=100.0)
    assert store.get("flight-tracker").expires_at == 3600.0

"""Time-limited claims on a project: "this is running for a demo, stop it
again at 14:35."

Everything else in this service keeps its state in memory and rebuilds it
on the next poll, which is fine when the worst case of losing it is a
re-baselined alert. Leases are the exception: forget one and a stack a
visitor started stays up indefinitely, burning the NAS's resources and
this deployment's OpenSky quota with nobody aware of it. So leases are
written to disk on every change.

A plain JSON file, not SQLite: there are at most a handful of these, they
are rewritten whole, and a file a human can `cat` while debugging why
something stopped itself is worth more here than query support.

Crash-safety comes from writing to a temp file in the same directory and
`os.replace`-ing it over the target, which is atomic on POSIX — a monitor
killed mid-write leaves either the old file or the new one, never a
truncated one that would fail to parse and lose every lease at once.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("docker_monitor")

OWNER_GUEST = "guest"
OWNER_ADOPTED = "adopted"


@dataclass(frozen=True)
class Lease:
    project: str
    started_by: str
    started_at: float
    expires_at: float

    def seconds_remaining(self, now: float = None) -> int:
        now = time.time() if now is None else now
        return max(0, int(self.expires_at - now))

    def is_expired(self, now: float = None) -> bool:
        now = time.time() if now is None else now
        return now >= self.expires_at

    def to_dict(self, now: float = None) -> dict:
        data = asdict(self)
        data["seconds_remaining"] = self.seconds_remaining(now)
        return data


class LeaseStore:
    def __init__(self, path: str):
        self._path = Path(path) if path else None
        self._leases: dict = {}
        self._load()

    # -- persistence ----------------------------------------------------

    def _load(self) -> None:
        if not self._path or not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            # A corrupt lease file must not stop the service from starting:
            # losing the leases means projects stay up too long, whereas
            # refusing to boot means nothing is monitored at all. The
            # startup reconciliation below re-adopts anything running, so
            # this degrades rather than orphans.
            logger.exception("Could not read lease file %s — starting with no leases", self._path)
            return
        for entry in raw or []:
            try:
                lease = Lease(**entry)
            except TypeError:
                logger.warning("Skipping malformed lease entry: %r", entry)
                continue
            self._leases[lease.project] = lease
        logger.info("Loaded %d lease(s) from %s", len(self._leases), self._path)

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", dir=self._path.parent, delete=False, encoding="utf-8"
            ) as handle:
                json.dump([asdict(lease) for lease in self._leases.values()], handle, indent=2)
                temp_name = handle.name
            os.replace(temp_name, self._path)
        except OSError:
            logger.exception("Could not persist leases to %s", self._path)

    # -- api ------------------------------------------------------------

    def grant(self, project: str, ttl_minutes: int, started_by: str = OWNER_GUEST, now: float = None) -> Lease:
        now = time.time() if now is None else now
        lease = Lease(
            project=project,
            started_by=started_by,
            started_at=now,
            expires_at=now + ttl_minutes * 60,
        )
        self._leases[project] = lease
        self._save()
        return lease

    def extend(self, project: str, ttl_minutes: int, now: float = None) -> Optional[Lease]:
        """Push an existing lease's expiry out from *now*. Used when a
        project that is already up is started again — the caller gets a
        fresh window rather than inheriting whatever was left of someone
        else's."""
        existing = self._leases.get(project)
        if existing is None:
            return None
        now = time.time() if now is None else now
        lease = Lease(
            project=project,
            started_by=existing.started_by,
            started_at=existing.started_at,
            expires_at=now + ttl_minutes * 60,
        )
        self._leases[project] = lease
        self._save()
        return lease

    def release(self, project: str) -> None:
        if self._leases.pop(project, None) is not None:
            self._save()

    def get(self, project: str) -> Optional[Lease]:
        return self._leases.get(project)

    def all(self) -> list:
        return list(self._leases.values())

    def as_dicts(self, now: float = None) -> dict:
        return {name: lease.to_dict(now) for name, lease in self._leases.items()}

    def active_count(self, now: float = None) -> int:
        now = time.time() if now is None else now
        return sum(1 for lease in self._leases.values() if not lease.is_expired(now))

    def expired(self, now: float = None) -> list:
        now = time.time() if now is None else now
        return [lease for lease in self._leases.values() if lease.is_expired(now)]


def reconcile(store: LeaseStore, views: list, default_ttl_minutes: int, now: float = None) -> list:
    """Bring lease state back in line with reality — called once at
    startup, before the first poll acts on anything.

    Two directions of drift, both of which actually happen:

    - **A lease for something that isn't running.** Someone stopped it by
      hand, or it crashed. The lease is meaningless; drop it, so its
      expiry doesn't later "stop" a project a human has since started for
      their own reasons.
    - **Something running with no lease.** The monitor was restarted (or
      crashed) while a demo was up, and the lease file was lost or never
      written. Left alone this runs forever, which is the exact failure
      the TTL exists to prevent — so adopt it with a default-length lease.
      Deliberately errs toward stopping a stack that might have been
      started by hand: for a guest-controllable demo project, "up longer
      than expected" is the failure mode with a real cost.

    Returns the names of projects adopted, for logging.
    """
    now = time.time() if now is None else now
    by_name = {view.name: view for view in views}
    adopted = []

    for lease in list(store.all()):
        view = by_name.get(lease.project)
        if view is None or view.state in ("stopped",):
            logger.info(
                "Releasing lease for %s: project is no longer running", lease.project
            )
            store.release(lease.project)

    for view in views:
        if view.always_on or not view.guest_controllable:
            continue
        if view.state in ("stopped",):
            continue
        if store.get(view.name) is not None:
            continue
        store.grant(view.name, default_ttl_minutes, started_by=OWNER_ADOPTED, now=now)
        adopted.append(view.name)
        logger.warning(
            "Adopted running project %s with a %d-minute lease: it was up with no "
            "lease on record (monitor restart?), and an un-leased demo project "
            "would otherwise run indefinitely",
            view.name,
            default_ttl_minutes,
        )
    return adopted

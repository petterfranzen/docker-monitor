# docker-monitor

A small, standalone service that watches Docker containers on a host and
sends an email when one breaks. Built for a home NAS running several
independent `docker compose` stacks (this dev machine's
[flight-tracker](../flight-tracker) is one of them) — nobody currently
finds out when a container silently crashes, restart-loops, or fails its
own healthcheck. This watches *any* container on the host by default; it
has no special knowledge of flight-tracker or any other specific stack.

## What it does

Every `POLL_INTERVAL_SECONDS`, it lists every container on the host (via
the Docker Engine API over the local socket) and checks each one for:

1. **Stopped unexpectedly** — a container that was running (or that
   defines `restart: always` / `unless-stopped`) is no longer running,
   including having vanished entirely (`docker rm`, or a container started
   with `--rm` that exited).
2. **Restart-looping** — Docker's own per-container restart counter has
   climbed by `RESTART_LOOP_THRESHOLD` or more within
   `RESTART_LOOP_WINDOW_SECONDS`.
3. **Unhealthy** — the container defines a Docker `HEALTHCHECK` and it's
   currently reporting `unhealthy`.

When one of these starts, it sends **one** email (or logs one line, in
dry-run mode) — not one per poll. If the condition clears, it optionally
sends a single "recovered" follow-up (`ALERT_ON_RECOVERY`). State is kept
in memory only, keyed by container *name* (not id), so recreating a
container under the same name — e.g. `docker compose up` after a `down` —
is correctly treated as "the same container recovering," not a new,
unrelated one.

A container that's already stopped when the monitor *starts* is not
treated as a problem (baseline, not a transition) — except a container
with `restart: always`/`unless-stopped` that's already down at startup,
which is flagged immediately, since that policy is a direct statement of
intent that it should be running.

**Not in scope for v1** (see "Decisions for the human" below): CPU/memory
resource-threshold alerting, a UI/dashboard, non-email notification
channels, and pausing alerts during a planned maintenance window.

## Tech stack — and why

**Python 3, using the `docker` SDK + stdlib `smtplib`.** No other runtime
dependency.

- The `docker` package is the standard, well-maintained Python client for
  the Docker Engine API (`containers.list()`/`.attrs` cover everything
  this needs: state, exit code, health, restart policy, restart count).
- `smtplib` + `email.message.EmailMessage` are stdlib — sending mail didn't
  need an extra dependency at all.
- This is a background poll loop with no HTTP surface of its own, so no
  web framework. A plain `while True: ... ; time.sleep(...)` loop is
  simpler and easier to reason about than pulling in a scheduler library
  for one job.
- No build step (unlike, say, TypeScript) — `pip install -r
  requirements.txt` and run. Keeps the Docker image small and the
  deploy loop fast.

Total third-party footprint: one package (`docker`, which itself pulls in
`requests`).

## Configuration (environment variables)

All of these live in `.env` (see `.env.example`, which documents each one
inline too).

| Variable | Default | Meaning |
|---|---|---|
| `NOTIFY_MODE` | `console` | `console` = dry-run, logs what would be emailed to stdout. `email` = actually send via SMTP. |
| `SMTP_HOST` | — | SMTP server hostname. **Required** if `NOTIFY_MODE=email`. |
| `SMTP_PORT` | `587` | SMTP port. |
| `SMTP_TLS_MODE` | `starttls` | `starttls`, `ssl`, or `none`. |
| `SMTP_USERNAME` | — | SMTP auth username (leave blank for an unauthenticated relay). |
| `SMTP_PASSWORD` | — | SMTP auth password / app password. **You must supply a real one** — none is invented or hardcoded here. |
| `SMTP_FROM` | — | From address. **Required** if `NOTIFY_MODE=email`. |
| `SMTP_TO` | — | Comma-separated recipient address(es). **Required** if `NOTIFY_MODE=email`. |
| `POLL_INTERVAL_SECONDS` | `30` | How often to check container state. |
| `RESTART_LOOP_THRESHOLD` | `3` | Restarts within the window below to call it a "restart loop." |
| `RESTART_LOOP_WINDOW_SECONDS` | `300` | Rolling window for the above. |
| `CONTAINER_INCLUDE` | *(empty = all)* | Comma-separated container names to watch; shell-glob patterns allowed (`flight-tracker-*`). |
| `CONTAINER_EXCLUDE` | *(empty)* | Same, but to exclude. Exclude wins over include on a name matching both. |
| `LABEL_INCLUDE` | *(empty)* | Comma-separated `key` or `key=value` — only watch containers carrying one of these labels. |
| `LABEL_EXCLUDE` | *(empty)* | Same, to exclude by label (e.g. `docker-monitor.ignore=true`). |
| `ALERT_ON_RECOVERY` | `true` | Send a follow-up email when a problem clears. |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. |

Real SMTP credentials are **not** provided or invented — you need to
supply your own (a Gmail account with an [App
Password](https://myaccount.google.com/apppasswords), or any SMTP relay
you already have — a NAS-attached mail relay, another provider, etc.) to
actually receive email. Until then, `NOTIFY_MODE=console` (the default)
lets you run and verify the whole pipeline for free.

## Running it locally

```bash
cp .env.example .env
# Leave NOTIFY_MODE=console to start — no SMTP setup required.
docker compose up -d --build
docker compose logs -f
```

To develop/test without Docker-in-Docker overhead, run it directly against
your host's Docker daemon:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export NOTIFY_MODE=console   # or source a local .env another way
python -m docker_monitor.main
```

### Unit tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

These test the alert/de-dup state machine (`docker_monitor/rules.py`)
against synthetic container snapshots — no real Docker daemon required.

### Verified against a live stack

While building this, `docker_monitor` was pointed at this dev machine's
own Docker daemon and run against the real, running
[flight-tracker](../flight-tracker) stack, in `NOTIFY_MODE=console` with a
5-second poll interval:

1. `docker stop flight-tracker-backend-agent-1` → one `PROBLEM` line
   logged within one poll cycle (`state=exited, exit_code=143,
   restart_policy=no`).
2. Left stopped for three more poll cycles → **no repeat** alert (de-dup
   confirmed).
3. `docker start flight-tracker-backend-agent-1` → one `RECOVERED` line
   logged within one poll cycle.
4. The flight-tracker stack was restored to its original running state
   afterward.

This also incidentally exercised the "container vanished" path live: an
unrelated, short-lived container in this shared dev environment was
removed mid-test and correctly triggered a `stopped` alert with detail
`container no longer exists on this host`.

One real finding from this test run: a container that was **already
stopped** before the monitor started (a stray, never-started leftover
container on this dev machine) was — correctly — not alerted on at
startup, but a bug in the initial baseline logic made it fire a spurious
alert on the *next* poll even though nothing had changed. Fixed (see git
history) and covered by a regression test
(`test_no_alert_on_second_poll_for_container_still_stopped_since_baseline`).

## NAS deployment

On the NAS, this runs the same way as any other Compose service already
does there (see `../flight-tracker/deploy/`), with one addition:

```bash
docker compose up -d --build
```

or build once elsewhere and push to a registry / GHCR the way
flight-tracker's `deploy/` does, if you'd rather not build on the NAS
itself.

**What's different from this dev machine:**

- **Real SMTP credentials.** Set `NOTIFY_MODE=email` and fill in `SMTP_*`
  in the NAS's `.env`. Test with `NOTIFY_MODE=console` first if you want to
  confirm it's picking up the NAS's containers correctly before wiring up
  real email.
- **A sane poll interval.** `30`–`60` seconds is plenty for a home NAS;
  the 5-second interval used above was only for fast local testing.
- **Scope.** With several stacks running in parallel, you may want to
  narrow scope with `CONTAINER_EXCLUDE`/`LABEL_EXCLUDE` for anything
  noisy or expected to cycle (e.g. a backup job container that's supposed
  to exit `0` every night — see "Decisions for the human" below).
- **The socket mount itself.** `/var/run/docker.sock:/var/run/docker.sock:ro`
  is the same on the NAS as here — it's host-Docker-daemon-specific, not
  per-stack, so this one `docker-monitor` container watches every stack on
  the NAS at once. You don't need one instance per stack.

### Security notes

- The socket is mounted **read-only** in the bind-mount sense
  (`:ro`) — but note this only stops the *bind mount itself* from being
  remounted read-write from inside the container; the Docker Engine API
  reachable over that socket is a full read/write control-plane API
  (start/stop/exec/create/delete anything on the host). This tool only
  ever calls the read endpoints (`list`, `inspect`), but nothing at the
  socket level *enforces* that — it's enforced by this codebase not
  calling anything else. Anyone who can exec into this container has the
  same host-level power any container with socket access does.
- `cap_drop: [ALL]` and `security_opt: [no-new-privileges:true]` in
  `docker-compose.yml` reduce what a compromised process here could do
  *beyond* the socket itself.
- The container process runs as root (see the comment in `Dockerfile` for
  why — matching the host's `docker` group GID generically isn't
  possible). If you want to eliminate the root-inside-container +
  full-API-surface combination entirely, the stronger option — used by
  tools like [diun](https://github.com/crazy-max/diun) in hardened setups
  — is a sidecar like
  [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy)
  in front of the real socket, restricted to just the `GET`
  `containers`/`images` endpoints this tool needs, with this service
  talking to the proxy instead of the raw socket. Not done here to keep v1
  simple (one extra container + network to reason about, for a home NAS
  that isn't multi-tenant) — worth adding later if that trade-off changes.

## Project layout

```
docker_monitor/
  config.py       # env var parsing (+ a tiny .env loader for local runs)
  dockerstate.py  # Docker SDK calls -> normalized ContainerSnapshot, include/exclude filtering
  rules.py        # the alert/de-dup state machine (pure, unit-tested, no Docker dependency)
  notifier.py     # console dry-run notifier + real SMTP notifier
  main.py         # poll loop entry point
tests/
  test_rules.py   # unit tests for rules.py
Dockerfile
docker-compose.yml
.env.example
```

## Decisions for the human (not made unilaterally)

- **Any unexpected stop is alertable, not just `restart: always`
  containers.** The spec emphasized `restart: always`/`unless-stopped`
  containers especially, but this defaults to alerting on *any* watched
  container that stops after being seen running — including a plain
  `docker stop`/`docker compose down`, since flight-tracker's own dev
  compose file doesn't set restart policies at all, and "someone meant to
  stop it" isn't distinguishable from "it crashed" purely from Docker
  state. If a stack has containers that are *supposed* to exit routinely
  (a one-shot migration job, a nightly backup container), add them to
  `CONTAINER_EXCLUDE`/`LABEL_EXCLUDE` — this doesn't try to guess that
  automatically.
- **Restart-loop threshold/window defaults** (`3` restarts / `300`
  seconds) are a starting guess, not a validated tuning — worth revisiting
  once there's a real restart-looping incident to calibrate against.
- **No maintenance-window/pause mechanism.** Taking a whole stack down
  intentionally (`docker compose down`) with the monitor still running
  will alert on every container in it. Options if that becomes annoying:
  a manual pause env var/signal, or just also stopping `docker-monitor`
  itself during planned maintenance.
- **Polling, not event streaming.** Docker's Engine API also offers an
  `/events` stream, which would catch state changes immediately instead of
  up to `POLL_INTERVAL_SECONDS` late and would avoid the sub-poll-interval
  blind spot (briefly seen in testing — see above). Polling was chosen for
  simplicity and resilience (a dropped event-stream connection needs its
  own reconnect/backlog logic; a poll loop just tries again next tick) but
  is a reasonable thing to revisit if faster detection matters more than
  simplicity.
- **Email only, for now.** Slack/Discord/webhook/ntfy channels would slot
  in easily behind the same `notifier.py` interface if wanted later.
- **CPU/memory resource-threshold alerting** was explicitly out of scope
  per the spec (stretch goal) and isn't implemented — worth a separate,
  deliberate design pass (thresholds, sustained-vs-spike detection) rather
  than bolting on quickly.

# docker-monitor

A small, standalone service that watches the Docker containers on a host,
pushes an [ntfy](https://ntfy.sh) notification when one breaks, and — via
a small HTTP API — starts, stops and updates the `docker compose` projects
it has been told it may control.

Built for a home NAS running several independent Compose stacks (this dev
machine's [flight-tracker](../flight-tracker) is one of them). It does two
jobs that share one view of the host:

1. **Alerting.** Nobody finds out when a container silently crashes,
   restart-loops, fails its own healthcheck, or goes quiet without
   actually stopping. This watches *any* container on the host by default
   and pushes when one breaks.
2. **Control.** Projects that don't need to run 24/7 shouldn't. The API
   lets something else — in practice, [the portfolio's
   dashboard](../portfolio) — show what's running, what it's *doing*, and
   start a stack on demand for a visitor, with a lease that stops it again
   an hour later whether or not anyone comes back.

The alerting half has no special knowledge of any stack. The control half
knows only what a registry file explicitly lists; see
[Controlling projects](#controlling-projects).

## What it does

Every `POLL_INTERVAL_SECONDS`, it lists every container on the host (via
the Docker Engine API over the local socket) and checks each one's
**state**:

1. **Stopped unexpectedly** — a container that was running (or that
   defines `restart: always` / `unless-stopped`) is no longer running,
   including having vanished entirely (`docker rm`, or a container started
   with `--rm` that exited).
2. **Restart-looping** — Docker's own per-container restart counter has
   climbed by `RESTART_LOOP_THRESHOLD` or more within
   `RESTART_LOOP_WINDOW_SECONDS`.
3. **Unhealthy** — the container defines a Docker `HEALTHCHECK` and it's
   currently reporting `unhealthy`.

It also tails each running container's **log output** (via the Engine
API's logs endpoint, not the `docker logs` CLI) looking for three things
state alone can't see:

4. **No data** — the container is running (maybe even reporting healthy)
   but has produced zero new log lines for longer than
   `NO_DATA_IDLE_SECONDS` — a common shape for something silently stuck.
5. **Known trouble text** — rate-limited/throttled upstream by default
   (`rate limit`, `throttl`, `too many requests`, `backing off`), plus
   anything else you configure (see "Log patterns" below).
6. **Traffic spike** — the container's log line rate has jumped past
   `TRAFFIC_SPIKE_MULTIPLIER`x its own recent baseline, sustained for 2+
   consecutive polls (so a normal startup burst doesn't trip it).

State problems (1–3) are **critical**; log-content problems (4–6) are
**warning** — this maps to ntfy priority/tags so the two classes are
distinguishable at a glance without opening the notification (see
"Severity → ntfy priority" below).

When any of these starts, it sends **one** push (or logs one line, in
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

**Still not in scope** (see "Decisions for the human" below): CPU/memory
resource-threshold alerting, non-ntfy notification channels, and pausing
alerts during a planned maintenance window. A UI is out of scope *here*
specifically — this serves an API; the dashboard that consumes it lives in
[the portfolio](../portfolio).

## Tech stack — and why

**Python 3, using the `docker` SDK.** No other runtime dependency.

- The `docker` package is the standard, well-maintained Python client for
  the Docker Engine API — `containers.list()`/`.attrs` cover container
  state, exit code, health, restart policy, and restart count;
  `container.logs(since=..., until=...)` covers log tailing, all via the
  same HTTP-over-socket API, never the `docker` CLI.
- Notifications go out via **ntfy** — a single HTTP POST (stdlib
  `urllib.request`, no client library needed) to a topic on a self-hosted
  ntfy server (bundled in `docker-compose.yml`) or any ntfy server you
  already run.
- The poll loop stays a plain `while True: ... ; sleep(...)` — simpler and
  easier to reason about than pulling in a scheduler library for one job.
  (It now runs as an asyncio task alongside the API rather than owning the
  process, but it's the same loop.) Log tailing is a periodic incremental
  fetch
  (`since=<last checkpoint>`) each poll cycle rather than a persistent
  streaming connection, for the same reason — it fits the existing
  poll-loop model and avoids a background thread + reconnect/backoff per
  container.
- **FastAPI + uvicorn** serve the control API. This is a deliberate break
  from the "one dependency" line above, and it cost something: the image
  is bigger and there's a web stack to keep current. The alternative was
  hand-rolling routing, concurrent request handling and Server-Sent Events
  on `http.server`, which is a few hundred lines of exactly the code
  everyone gets subtly wrong. The poll loop itself didn't change character
  — it runs as an asyncio task in the same process, so this is still one
  container doing one job, and `API_ENABLED=false` skips the web stack
  entirely for an alerting-only instance.
- The image also carries the **`docker compose` CLI**, copied from
  Docker's own published CLI image, for lifecycle operations — see
  [Why the Compose CLI](#why-the-compose-cli).
- No build step (unlike, say, TypeScript) — `pip install -r
  requirements.txt` and run.

Third-party footprint: `docker` (which pulls in `requests`), plus
`fastapi`/`uvicorn` for the API.

## Configuration (environment variables)

All of these live in `.env` (see `.env.example`, which documents each one
inline too).

| Variable | Default | Meaning |
|---|---|---|
| `NOTIFY_MODE` | `console` | `console` = dry-run, logs what would be pushed to stdout. `ntfy` = actually push. |
| `NTFY_URL` | `http://ntfy:80` | Base URL of the ntfy server. Default reaches the bundled `ntfy` compose service; point elsewhere to use an existing server. |
| `NTFY_TOPIC` | — | Topic to publish to. **Required** if `NOTIFY_MODE=ntfy`. Treat it like a shared secret — see "Security notes." |
| `NTFY_PORT` | `8080` | Host port the bundled ntfy service publishes on (phone/web app, local testing). |
| `POLL_INTERVAL_SECONDS` | `30` | How often to check container state. |
| `RESTART_LOOP_THRESHOLD` | `3` | Restarts within the window below to call it a "restart loop." |
| `RESTART_LOOP_WINDOW_SECONDS` | `300` | Rolling window for the above. |
| `CONTAINER_INCLUDE` | *(empty = all)* | Comma-separated container names to watch; shell-glob patterns allowed (`flight-tracker-*`). |
| `CONTAINER_EXCLUDE` | *(empty)* | Same, but to exclude. Exclude wins over include on a name matching both. |
| `LABEL_INCLUDE` | *(empty)* | Comma-separated `key` or `key=value` — only watch containers carrying one of these labels. |
| `LABEL_EXCLUDE` | *(empty)* | Same, to exclude by label (e.g. `docker-monitor.ignore=true`). |
| `LOG_MONITORING_ENABLED` | `true` | Global on/off switch for all of items 4–6 above. |
| `NO_DATA_IDLE_SECONDS` | `600` | Idle threshold for the "no data" check. |
| `NO_DATA_EXCLUDE` | *(empty)* | Names/globs to skip "no data" checking for (containers that are legitimately quiet, e.g. a database with no active connections). |
| `LOG_PATTERN_RATE_LIMITED_ENABLED` | `true` | Built-in rate-limit/throttle text match. |
| `LOG_PATTERN_GENERIC_ERROR_ENABLED` | `false` | Built-in `error`/`exception`/`fatal` catch-all — off by default, noisy on plenty of apps. |
| `LOG_PATTERNS_FILE` | *(empty)* | Path to a JSON file of additional/custom patterns — see "Log patterns" below. |
| `TRAFFIC_BASELINE_WINDOW_SECONDS` | `1800` | Rolling window the traffic-rate baseline is computed over. |
| `TRAFFIC_SPIKE_MULTIPLIER` | `5` | Alert when current rate exceeds baseline by this multiplier. |
| `TRAFFIC_MIN_BASELINE_SAMPLES` | `5` | Minimum "normal" polls needed before a baseline is trusted. |
| `TRAFFIC_GRACE_PERIOD_SECONDS` | `300` | Skip spike detection until a container's been watched this long (avoids startup bursts). |
| `TRAFFIC_MIN_RATE_LINES_PER_MIN` | `2` | Floor so a near-zero baseline doesn't trivially count as "5x." |
| `ALERT_ON_RECOVERY` | `true` | Send a follow-up push when a problem clears. |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. |
| `PHASE_TRACKING_ENABLED` | `true` | Parse `[phase:…]` markers out of container logs — see "Phase reporting". |
| `PHASE_STALE_SECONDS` | `900` | A phase older than this is reported as unknown rather than current. |
| `API_ENABLED` | `true` | Serve the control API. `false` = alerting only, nothing listens on a port. |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | Where the API listens. |
| `PROJECTS_FILE` | *(empty)* | JSON registry of controllable projects. **Without it nothing can be started or stopped** — see "Controlling projects". |
| `LEASES_FILE` | `/data/leases.json` | Where demo leases persist. Must be on a volume that outlives the container. |
| `CONTROL_TOKEN` | *(empty)* | Bearer token for owner operations. Empty = those are refused for everyone. `openssl rand -hex 32`. |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Read by both docker-py and the bundled Compose CLI. On the NAS: `tcp://socket-proxy:2375`. |
| `ACTIVE_POLL_INTERVAL_SECONDS` | `5` | Poll interval used while a project is mid-operation or holding a lease, so a dashboard updates believably. |
| `DEFAULT_TTL_MINUTES` | `60` | How long a guest's demo runs before it's stopped automatically. |
| `MAX_CONCURRENT_GUEST_PROJECTS` | `1` | How many guest-started projects may run at once. |
| `MAX_CONCURRENT_OPERATIONS` | `2` | How many compose operations may run in parallel. |
| `TRUST_PROXY_HEADERS` | `false` | Believe `X-Forwarded-For`. Only turn on behind a reverse proxy you control — see "Security notes". |
| `GUEST_RATE_WINDOW_SECONDS` | `600` | Window for the per-IP request limit below. |
| `GUEST_MAX_REQUESTS_PER_WINDOW` | `3` | Control requests one address may make per window. |
| `GUEST_MAX_REQUESTS_PER_DAY` | `20` | Control requests one address may make per day. |

No ntfy credentials are invented here — `NTFY_TOPIC` is yours to pick.
Until you set one, `NOTIFY_MODE=console` (the default) lets you run and
verify the whole pipeline for free.

### Log patterns

Beyond the two built-in toggles, `LOG_PATTERNS_FILE` points at a JSON file
of additional patterns, each optionally scoped to specific containers:

```json
[
  {
    "label": "db_conn_lost",
    "severity": "critical",
    "pattern": "connection refused|connection reset",
    "containers": ["flight-tracker-backend-*"]
  }
]
```

`containers` is optional (omit to apply to every watched container);
`"enabled": false` disables an entry without deleting it. `pattern` is a
Python regex, matched case-insensitively against each new log line.

Note on matching bare HTTP status codes: the built-in rate-limited pattern
deliberately does **not** match a bare `429` — found live, during this
project's own testing, that a plain `\b429\b` regex reliably
false-positives against ordinary log noise (it matched, of all things, the
*milliseconds field of a log line's own timestamp*, e.g. `10:01:22,429`).
A bare 3-digit number is too short and common a token in unstructured text
to be a safe default. If you want to match a specific status code for your
own app's actual log format, scope it tightly, e.g.
`"pattern": "\\bhttp\\S{0,12}\\s*429\\b"` — "429 near the word http" is far
less likely to collide with a timestamp or port number than "429" alone.

### Severity → ntfy priority

| | Priority | Tag |
|---|---|---|
| Critical problem (stopped/unhealthy/restart-loop) | 5 | 🚨 `rotating_light` |
| Critical recovered | 3 | ✅ `white_check_mark` |
| Warning problem (no-data/rate-limited/traffic-spike/custom) | 3 | ⚠️ `warning` |
| Warning recovered | 2 | ✅ `white_check_mark` |

## Phase reporting

Docker knows a container is "running" and, if it defines a HEALTHCHECK,
"healthy". Neither tells you that flight-tracker's agent is a third of the
way through backfilling positions — and for a dashboard where somebody is
waiting on a demo to become usable, that's the only interesting part.

So apps say what they're doing, on their own stdout:

```
[phase:populating_data] global sweep 1/3
```

docker-monitor parses those out of the log lines it is *already* fetching
for the checks above — no status endpoint per app, no extra Docker calls,
and an app that says nothing simply has no phase, which is an honest
answer rather than a guess.

| Phase | Means |
|---|---|
| `starting_up` | Booting; not usable yet. |
| `populating_data` | Doing real work — fetching, importing, backfilling. |
| `ready` | Up and serving. |
| `idle` | Up, nothing to do. |
| `degraded` | Running but impaired (upstream rate-limited, a dependency down). |
| `shutting_down` | On the way out. |

Anything outside that list is ignored rather than passed through: this is
a protocol between the apps and the dashboard, and a UI can't rank a phase
it has never heard of. The detail text after the marker is free-form and
shown as-is.

**Two rules for apps emitting these** (see the implementations in
[flight-tracker](../flight-tracker) and [dinner-planner](../dinner-planner)):

1. **Emit on transition only**, never once per loop iteration. A marker
   per iteration of a few-second refresh loop buries the real logs and
   trips the traffic-spike detector above.
2. **Never re-emit someone else's phase in marker form.** docker-monitor
   itself follows this: when it logs another container's phase it writes
   `phase=populating_data container=…`, not the `[phase:…]` marker. Its
   own stdout is a container log stream too, and echoing the marker would
   make a monitor watching a monitor attribute the phase to the wrong
   thing — the same self-referential feedback shape as the rate-limit
   false positive documented above.

A phase older than `PHASE_STALE_SECONDS` (default 900) is treated as
unknown rather than current: a container that said "populating_data" an
hour ago and has been silent since is not still populating data.

## Controlling projects

### What a "project" is

A Compose project — the thing `docker compose ls` lists and the unit a
person actually thinks in ("start the flight tracker"), rather than the
five containers underneath it. Grouping is automatic, from the
`com.docker.compose.project` label Compose puts on everything it creates.

### The registry, and why it exists

Discovery alone isn't enough for control, for two reasons: a project that
is fully *down* has no containers to read labels off, and nothing in
Docker says who is allowed to start what. So `PROJECTS_FILE` points at a
JSON registry — see [`projects.example.json`](projects.example.json) for a
documented, working example:

```json
{
  "flight-tracker": {
    "display_name": "Flight Tracker",
    "compose_file": "/stacks/flight-tracker/docker-compose.yml",
    "demo_url": "http://nas.local:8090",
    "guest_controllable": true,
    "default_ttl_minutes": 60,
    "max_ttl_minutes": 120,
    "ready_container": "flight_tracker_frontend"
  }
}
```

**The compose file must run from prebuilt images.** This never builds
anything — there's no build toolchain in the image, and the socket proxy
sets `BUILD=0` — so a compose file using `build:` fails at `up`. Point
`compose_file` at each project's `deploy/` compose file (which pulls from
GHCR), not the repo-root one most of them use for local development. A
project that has no `deploy/` compose file yet can't be controlled from
here at all, however it's configured.

**A project not in the registry is visible but never controllable.** If
it's running on the host it shows up in the API read-only; nothing can
start or stop it. That asymmetry is the main safety property here — a
public endpoint must not be able to touch an arbitrary stack on the NAS,
and "is it listed?" is a much simpler question to get right than "should
this caller be allowed to do this?".

### Guests and owners

| | Guest (no token) | Owner (`Authorization: Bearer $CONTROL_TOKEN`) |
|---|---|---|
| Read status | ✅ | ✅ |
| Start/stop `guest_controllable` projects | ✅ (rate-limited, capped, leased) | ✅ |
| Start/stop anything else controllable | ❌ | ✅ |
| Restart | ❌ | ✅ |
| Update (pull + recreate) | ❌ | ✅ |

With no `CONTROL_TOKEN` set, owner operations are refused for *everyone*.
"No token" never means "everyone is the owner".

Guests are additionally bounded by:

- **A lease.** Starting a project grants one (`DEFAULT_TTL_MINUTES`,
  default 60, clamped to the project's `max_ttl_minutes`). When it
  expires, the project is stopped. This is the backstop the whole feature
  rests on, so leases are persisted to disk — a monitor restart must not
  orphan a running stack. On startup anything running without a lease is
  *adopted* with a default-length one, deliberately erring toward stopping
  a stack that might have been started by hand.
- **A concurrency cap** (`MAX_CONCURRENT_GUEST_PROJECTS`, default 1).
- **Per-IP rate and daily limits**, in the same shape flight-tracker
  already uses for its public `POST /api/agents/restart`. Callers on a
  private/loopback address are exempt from all of it — the owner testing
  from their own LAN isn't who any of this exists for.

### Endpoints

| Endpoint | Auth | Notes |
|---|---|---|
| `GET /api/projects` | none | Every project, with state, phase and lease. |
| `GET /api/projects/{name}` | none | Adds per-container detail and the last operation's result. |
| `GET /api/events` | none | Server-Sent Events: full state on connect, again on every change, heartbeat every 15s. |
| `POST /api/projects/{name}/start` | guest | Body `{"ttl_minutes": 60}`, optional. |
| `POST /api/projects/{name}/stop` | guest | |
| `POST /api/projects/{name}/restart` | owner | |
| `POST /api/projects/{name}/update` | owner | `pull`, then `up -d`. |
| `GET /healthz` | none | |
| `GET /api/docs` | none | Generated OpenAPI browser. |

Start/stop/restart/update all return immediately and do the work in the
background — `docker compose up -d` on a cold stack takes tens of seconds,
well past any sensible proxy timeout. Watch `/api/events` (or poll) for
the result; the project reports `starting`/`stopping` while an operation
is in flight, and a second operation on a busy project is refused with a
409 rather than racing the first.

### Why the Compose CLI

Lifecycle operations shell out to `docker compose`, which is the one place
this service doesn't use the Engine API directly. "Update" honestly means
"pull a newer image and recreate the container with the same
configuration", and doing that through the raw API means re-deriving a
container's full create spec from its inspect output — networks, aliases,
mounts, env, every Compose label — Watchtower-style. Compose already does
that correctly, `docker compose pull && up -d` is literally the documented
update procedure in flight-tracker's own deploy README, and using the same
command a human would use keeps the two from drifting apart.

The CLI still never touches the host socket: `DOCKER_HOST` points at the
socket proxy, same as docker-py.

Note that **stop uses `stop`, not `down`**. `down` removes containers and
would leave the next start re-initialising an empty database — every demo
would then sit in "populating data" from scratch.

## ntfy setup

### Subscribing on a phone

1. Install the ntfy app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)).
2. Add a subscription → "Use a different server" → enter
   `http://<nas-ip>:<NTFY_PORT>` (default port `8080`, or wherever you
   mapped it — see NAS deployment below for why `http://<nas-ip>` and not
   `http://ntfy:80`, which only resolves inside the compose network).
3. Enter the exact `NTFY_TOPIC` value from your `.env`.

The [web app](https://docs.ntfy.sh/subscribe/web/) works the same way,
pointed at the same URL, from a browser — useful for the initial
`NOTIFY_MODE=console` → `ntfy` switchover before installing anything on a
phone.

### Security notes

**ntfy topics are unauthenticated-readable (and, by default,
publishable) by anyone who knows the URL and topic name.** There's no
account system on a topic by default — it's whoever's subscribed. Two real
options ntfy actually supports, from least to most effort:

- **Treat the topic name as a shared secret.** Pick something long and
  hard-to-guess (`docker-monitor-<random string>`, not `alerts`), don't
  publish it anywhere public, and this is reasonably safe for a topic that
  never leaves your LAN/VPN.
- **Turn on ntfy's access control.** ntfy supports requiring a
  username/password or access token per topic — you run the server with
  an auth file enabled and set access levels with the `ntfy user` /
  `ntfy access` commands (or a generated token via `ntfy token`), then the
  monitor and your phone both need to authenticate to read/write that
  topic. This is the right call if the bundled ntfy port ends up reachable
  from beyond your LAN (e.g. port-forwarded or exposed via a reverse
  proxy). Full details: <https://docs.ntfy.sh/config/#access-control>.

This project doesn't configure either for you — pick based on your actual
exposure (LAN-only vs. reachable from the internet) rather than defaulting
to the more complex option unconditionally.

## Running it locally

```bash
cp .env.example .env
cp projects.example.json projects.json   # edit: what may be controlled
# Leave NOTIFY_MODE=console to start — no ntfy server needed yet.
docker compose up -d --build
docker compose logs -f docker-monitor
curl localhost:8000/api/projects         # the control API
```

The compose files of whatever you list in `projects.json` need to be
readable inside the container — point `STACKS_DIR` at the directory
holding them (it's mounted read-only at `/stacks`).

This also brings up the bundled `ntfy` service (published on
`NTFY_PORT`, default `8080`). To actually receive pushes: set
`NOTIFY_MODE=ntfy` and `NTFY_TOPIC` in `.env`, `docker compose up -d`
again, then subscribe to that topic at `http://localhost:$NTFY_PORT` (web
app) or from your phone on the same LAN.

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

106 tests, no real Docker daemon needed by any of them. The original 19
cover the container-state de-dup state machine (`test_rules.py`), the
log-content watcher's three checks (`test_logwatch.py`, using a
`FakeContainer` stub) and the ntfy severity→priority/tag mapping
(`test_notifier.py`).

The rest cover the control plane, weighted toward the things whose failure
is expensive rather than merely annoying:

- `test_api.py` — the guest/owner boundary, driven end-to-end through the
  real FastAPI app over a fake daemon. A guest can't restart, can't
  update, can't start a project that isn't marked `guest_controllable`,
  can't get past the concurrency cap or the rate limit; an absent
  `CONTROL_TOKEN` disables owner operations rather than enabling them for
  everyone; and an expired lease stops the project.
- `test_leases.py` — the TTL backstop and the two ways it silently fails:
  state lost across a restart, and a stack running with no lease at all.
- `test_phases.py` — marker parsing, the closed vocabulary, staleness, and
  the no-echo rule (a rendered phase must not parse back as a marker).
- `test_projects.py` — grouping, registry loading, and that a project
  discovered on the host is visible but never controllable.
- `test_lifecycle.py` — the exact compose command each operation would
  run, failure handling, and that a second operation on a busy project is
  refused.

One thing deliberately isn't unit-tested: the live SSE stream. Starlette's
`TestClient` drives the app through a blocking portal, and a
`StreamingResponse` that never ends by design has no clean client-side
teardown — the test hangs rather than fails. The frame *format* is
unit-tested; the stream itself is verified with `curl -N` below.

### Verified against a live stack

Container-state detection (stopped/recovered/de-dup) was verified against
this dev machine's real, running [flight-tracker](../flight-tracker) stack:

1. `docker stop flight-tracker-backend-agent-1` → one `PROBLEM` push
   (priority 5) within one poll cycle.
2. Left stopped for several more poll cycles → **no repeat** push
   (de-dup confirmed).
3. `docker start flight-tracker-backend-agent-1` → one `RECOVERED` push
   (priority 3, correctly de-escalated from the problem's priority 5)
   within one poll cycle.
4. The flight-tracker stack was restored to its original running state
   afterward.

This also incidentally exercised the "container vanished" path: an
unrelated, short-lived container in this shared dev environment was
removed mid-test and correctly triggered a `stopped` push.

Log-content detection was verified end-to-end against the bundled,
self-hosted ntfy instance (no real phone needed — confirmed via ntfy's own
`/json?poll=1&since=all` message-history endpoint) using a disposable test
container, since flight-tracker's `backend-agent` wasn't actually being
rate-limited by OpenSky at test time (checked its live logs first, per the
plan — its OpenSky polling doesn't always trip that condition, so this
synthesized it instead, as a real container's own log stream, not
`docker exec`, which writes to a separate exec session rather than the
container's actual PID 1 output the Engine API's logs endpoint reads):

1. A test container logged a few normal lines, then one containing
   `"backing off (rate limit exceeded, too many requests)"`.
2. `docker-monitor` fired one `PROBLEM` push (priority 3, `⚠️`) within
   one poll cycle, detail `matched: 'WARN openSkyClient: ...'`.
3. The next poll's lines were all normal again → one `RECOVERED` push
   (priority 2, `✅`) fired, confirmed via ntfy's message history.

The idle ("no data") and traffic-spike checks were verified with unit
tests using synthetic containers (a controllable `now` and canned log
batches) rather than a live daemon, per plan — `test_logwatch.py` covers
an idle-then-active container hitting and clearing the no-data threshold,
a container excluded from no-data checking never alerting, and a
sustained 2-poll traffic burst firing once and recovering once.

**Two real bugs were found and fixed during this live verification** (not
caught by the offline unit tests, which use synthetic data that doesn't
exhibit either failure mode):

1. **Baseline-diffing bug** (container-state engine): a container already
   stopped when monitoring started got its baseline recorded as an *empty*
   alert set instead of its real state, making the very next poll — still
   stopped, nothing changed — look like a fresh transition and fire a
   spurious `PROBLEM`. Fixed by recording ground truth on the baseline poll
   instead of diffing against empty. Regression test:
   `test_no_alert_on_second_poll_for_container_still_stopped_since_baseline`.
2. **Self-referential feedback + bare-429 false positive** (log-content
   watcher): the bare `\b429\b` alternative in the default rate-limited
   pattern matched docker-monitor's own log line on the milliseconds field
   of its own timestamp; separately, docker-monitor's own notification
   logs echo back the container names/alert labels it reports on, and
   since it log-content-watches itself by default, that self-printed text
   could match its own patterns and alert about itself — reproduced when a
   test container's name happened to contain "ratelimit." Fixed by
   dropping the bare-429 alternative (see "Log patterns" above) and
   excluding docker-monitor's own container from log-content watching
   specifically (state watching is unaffected — Docker's own state carries
   no alert-describing text, so it has no equivalent feedback path).

### Verifying the control API

The control plane was verified against this dev machine's real Docker
daemon (29.8.1 / Compose 5.5.1), driving a throwaway Compose project that
prints phase markers on a schedule, rather than only against the fakes in
`test_api.py`:

1. `GET /api/projects` listed the registry project as `stopped` and
   `controllable` while no container for it existed at all — the case that
   makes the registry necessary in the first place.
2. `POST .../start` with `{"ttl_minutes": 2}` returned immediately with a
   lease; the project reported `starting` while `docker compose up -d`
   ran, then `running`.
3. Its phase moved `starting_up` → `populating_data` → `idle` as the
   container logged each marker, with the detail text (`"fetching window
   1/3"`) carried through. `demo_ready` was `false` during `starting_up`
   and `true` from `populating_data` on — i.e. Docker health alone would
   have said "ready" a poll earlier, which is the exact lie the phase
   check exists to catch.
4. **The first marker was picked up on the container's first sighting**,
   not the one after, confirming the backfill in `logwatch.py` — without
   it a freshly started stack shows no phase for up to two poll intervals,
   which is most of the time anyone is actually watching.
5. `curl -N /api/events` delivered the initial state, further frames as
   state changed, and `: keep-alive` heartbeats in between.
6. Auth: `update` without a token → 401, with a wrong token → 403, with
   the right one → 200; an unknown project → 404.
7. **The lease expired and the project stopped itself** — `running` →
   `stopping` → `stopped`, the lease released, with
   `Lease for demo-stack expired after 2 minutes — stopping it` in the
   log. This is the single behaviour the guest-start feature rests on.
8. Restarting docker-monitor with the stack up and the lease file deleted
   → the project was **adopted** with a fresh 60-minute lease, rather than
   left running indefinitely.
9. Starting an already-running project extended its lease *without*
   recreating the container (verified by comparing `StartedAt` before and
   after) — a visitor arriving mid-demo shouldn't restart it under the
   person already using it.
10. **Alerting still works alongside all of this**: the auto-stop in step 7
    produced exactly one `PROBLEM: demo_stack_worker (stopped)` push at
    priority 5, unchanged from before this feature existed.


## NAS deployment

On the NAS, this runs the same way as any other Compose service already
does there (see `../flight-tracker/deploy/`), with one addition:

```bash
docker compose up -d --build
```

or, since this repo now has the same "build once in CI, pull on the NAS"
pipeline flight-tracker's `deploy/` does: pull the prebuilt
`ghcr.io/petterfranzen/docker-monitor` image instead of building on the
NAS itself. Use `deploy/docker-compose.yml` and `deploy/.env.example`
in place of the root files, and see `deploy/README.md` for the full
walkthrough (publishing, making the GHCR package pullable, and running
it).

**What's different from this dev machine:**

- **A real ntfy topic, subscribed from your phone.** Set `NOTIFY_MODE=ntfy`
  and a hard-to-guess `NTFY_TOPIC` in the NAS's `.env`. Test with
  `NOTIFY_MODE=console` first if you want to confirm it's picking up the
  NAS's containers correctly before wiring up real pushes.
- **`NTFY_URL` from your phone's perspective.** `http://ntfy:80` (the
  default) is a compose-internal hostname — docker-monitor itself uses it
  fine, but your phone needs `http://<nas-ip>:<NTFY_PORT>` instead, since
  it's not on the compose network.
- **A sane poll interval.** `30`–`60` seconds is plenty for a home NAS;
  the 5-second interval used in testing above was only for fast iteration.
- **Scope.** With several stacks running in parallel, you may want to
  narrow scope with `CONTAINER_EXCLUDE`/`LABEL_EXCLUDE`, and
  `NO_DATA_EXCLUDE` for containers that are legitimately quiet — see
  "Decisions for the human" below.
- **Already run an ntfy server on the NAS?** Point `NTFY_URL` at it and
  delete/comment out the bundled `ntfy` service in `docker-compose.yml` —
  one ntfy server is plenty for a whole NAS, you don't need a second one
  per monitored stack.
- **How it reaches Docker.** On the NAS there is no socket mount at all:
  `deploy/docker-compose.yml` runs a socket-proxy sidecar and points
  `DOCKER_HOST` at it. Either way it's host-daemon-specific, not
  per-stack, so this one `docker-monitor` container watches (and controls)
  every stack on the NAS at once. You don't need one instance per stack.
- **The registry and your stacks directory.** Control needs two read-only
  mounts that the dev compose file fakes with examples: a
  `projects.json` (start from `projects.example.json`) and the directory
  holding each project's compose file. Without them, projects are listed
  but nothing can be started.

### Security notes

**This service can now start and stop containers, and is meant to be
reachable from the public internet. Read this section before deploying it
that way.**

- **The socket is no longer read-only, because it can't be.** Starting a
  container is a write operation. Earlier versions mounted the socket
  `:ro` and only called `list`/`inspect`/`logs`; that's no longer true, and
  pretending otherwise would be the dangerous kind of stale documentation.
- **The socket proxy helps, but it is not the boundary.**
  `deploy/docker-compose.yml` puts
  [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy)
  in front of the daemon, and docker-monitor never sees
  `/var/run/docker.sock` at all. That blocks `exec` (the difference
  between managing containers and running arbitrary commands inside any of
  them, including ones holding secrets), plus build, swarm, secrets and
  configs. But `CONTAINERS=1` + `POST=1` — which `docker compose up`
  requires — is still enough to create a privileged container, and a
  privileged container is the host. **Treat the proxy as narrowing the
  blast radius of a compromised monitor, not as containing one.**
- **The actual boundary for public traffic is the API.** Two things do the
  work: a caller without the `CONTROL_TOKEN` can only touch projects
  explicitly marked `guest_controllable` in the registry, and the registry
  is a read-only mount this process cannot rewrite. No endpoint accepts a
  compose file path, a container name, or a command — callers name a
  project, and the project must already be listed. Everything else (rate
  limits, the concurrency cap, the TTL) is about cost and abuse, not about
  containment.
- **`TRUST_PROXY_HEADERS` is off by default and should stay off unless you
  have a reverse proxy.** With the API port reachable directly, a client
  that can set its own `X-Forwarded-For` gets a fresh identity per request
  and walks straight past every per-IP limit. Turn it on only when
  something you control terminates connections in front of it — the
  portfolio's nginx does, which is why the dashboard reaches this at
  `/lab-api/` rather than by its own port.
- **Set a `CONTROL_TOKEN`.** Without one, `restart` and `update` are
  refused for everybody (the safe direction, but probably not what you
  want). Generate it with `openssl rand -hex 32`; don't reuse anything.
- **The `CONTROL_TOKEN` is a bearer token over whatever transport you put
  this behind.** On plain HTTP over a LAN that's a reasonable trade; if
  this is reachable from the internet, terminate TLS in front of it, or
  the token is readable by anything on the path.
- `cap_drop: [ALL]` and `security_opt: [no-new-privileges:true]` in
  `docker-compose.yml` (on both `docker-monitor` and the bundled `ntfy`
  service) reduce what a compromised process here could do *beyond* the
  socket itself.
- The `docker-monitor` process runs as root (see the comment in
  `Dockerfile` for why — matching the host's `docker` group GID generically
  isn't possible). If you want to eliminate the root-inside-container +
  full-API-surface combination entirely, the stronger option — used by
  tools like [diun](https://github.com/crazy-max/diun) in hardened setups
  — is a sidecar like
  [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy)
  in front of the real socket, restricted to just the `GET`
  `containers`/`logs` endpoints this tool needs, with this service talking
  to the proxy instead of the raw socket. Not done here to keep v1 simple
  (one extra container + network to reason about, for a home NAS that
  isn't multi-tenant) — worth adding later if that trade-off changes.
- See "ntfy setup → Security notes" above for the topic-access question,
  which is a separate concern from the socket.

## Project layout

```
docker_monitor/
  config.py       # env var parsing (+ a tiny .env loader for local runs)
  dockerstate.py  # Docker SDK calls -> normalized ContainerSnapshot, include/exclude filtering
  rules.py        # container-state alert/de-dup state machine (pure, unit-tested, no Docker dependency)
  logwatch.py     # log-content watcher: no-data, patterns, traffic-spike; also the one log fetch phases ride on
  notifier.py     # console dry-run notifier + real ntfy notifier, severity -> priority/tags
  phases.py       # [phase:...] marker parsing, per-container tracking, project aggregation
  projects.py     # compose-label grouping + the registry -> the project view the API serves
  leases.py       # time-limited demo claims, persisted; startup reconciliation
  lifecycle.py    # start/stop/restart/update via the `docker compose` CLI, off the request thread
  ratelimit.py    # per-IP guest limits and client-IP resolution
  service.py      # one poll feeding alerting, phases and the project view; the control entry points
  api.py          # FastAPI app: read endpoints, SSE, control endpoints, guest/owner split
  main.py         # entry point: uvicorn + poll task, or the plain poll loop if API_ENABLED=false
tests/
  test_rules.py     # container-state alert/de-dup state machine
  test_logwatch.py  # log-content checks (FakeContainer stub, no real Docker daemon)
  test_notifier.py  # priority/tag mapping
  test_phases.py    # marker parsing, staleness, aggregation, the no-echo rule
  test_projects.py  # grouping, registry loading, project state, demo_ready
  test_leases.py    # TTL expiry, persistence across restart, reconciliation
  test_lifecycle.py # the compose commands that would run, failures, concurrency
  test_api.py       # the guest/owner boundary end-to-end over a fake daemon
Dockerfile
docker-compose.yml       # dev: builds from source, host socket, API on localhost
projects.example.json    # documented example of the project registry
deploy/                  # NAS: prebuilt image + socket proxy
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
- **No-data/traffic-spike thresholds are equally unvalidated guesses**
  (`600`s idle, `5`x/2-poll spike, `300`s grace period). These are
  generic-by-design (no per-app tuning required to get *something*
  useful), but a chatty container's "normal" might be your quiet
  container's "spike" — expect to tune `NO_DATA_IDLE_SECONDS`,
  `TRAFFIC_SPIKE_MULTIPLIER`, and `NO_DATA_EXCLUDE` per real containers
  once you see how noisy (or not) they are in practice.
- **The generic `error`/`exception`/`fatal` catch-all defaults off**, and
  the built-in rate-limited pattern deliberately excludes bare `429` (see
  "Log patterns" above) — both because unstructured-log keyword matching
  has a real, demonstrated false-positive rate against ordinary log noise.
  Turning on the generic catch-all, or adding your own patterns via
  `LOG_PATTERNS_FILE`, is a per-app judgment call this tool doesn't try to
  make for you.
- **docker-monitor excludes only itself from log-content watching**, not
  from state watching — see the README section above on the
  self-referential feedback bug found during testing. If you run a
  *second* docker-monitor instance to watch the first one, that second
  instance's self-exclusion only covers itself, not the first instance;
  they don't know about each other.
- **No maintenance-window/pause mechanism.** Taking a whole stack down
  intentionally (`docker compose down`) with the monitor still running
  will alert on every container in it. Options if that becomes annoying:
  a manual pause env var/signal, or just also stopping `docker-monitor`
  itself during planned maintenance.
- **Polling, not event/log streaming.** Docker's Engine API also offers an
  `/events` stream (for state) and a `follow=true` log stream, either of
  which would catch changes immediately instead of up to
  `POLL_INTERVAL_SECONDS` late. Polling was chosen for simplicity and
  resilience (a dropped streaming connection needs its own
  reconnect/backlog logic; a poll loop just tries again next tick) but is
  a reasonable thing to revisit if faster detection matters more than
  simplicity.
- **ntfy only, for now.** Slack/Discord/webhook/email channels would slot
  in easily behind the same `notifier.py` interface if wanted later.
- **Lifecycle is `stop`, never `down`.** Stopping a project leaves its
  containers and volumes in place, so the next start is fast and
  flight-tracker's accumulated position history survives. The cost is that
  a project stopped this way still holds disk. If you want a true teardown,
  that's a deliberate `docker compose down` by hand — this API won't do it,
  because a guest-reachable endpoint that can delete volumes is a
  different risk category entirely.
- **A guest can stop a demo someone else started.** With a concurrency cap
  of 1 and no accounts, the alternative is worse: a visitor who closes the
  tab would otherwise block everyone for the rest of the TTL. Revisit if
  it ever gets used enough for that to be annoying.
- **Adoption errs toward stopping things.** A running, guest-controllable
  project with no lease on record gets one at startup, which means a stack
  *you* started by hand can be stopped an hour later if it happens to be
  in the registry as guest-controllable. Mark anything that should stay up
  `always_on`, which exempts it from leases entirely.
- **No per-guest identity.** Rate limits are per IP, which is
  approximately right for home-scale traffic and wrong behind carrier NAT
  or a shared office address. The TTL and concurrency cap are the real
  cost ceiling; the per-IP limits just make casual abuse tedious.
- **ntfy topic access control is documented, not configured.** This
  project doesn't decide LAN-only vs. internet-reachable for you — see
  "ntfy setup → Security notes."
- **CPU/memory resource-threshold alerting** was explicitly out of scope
  per the spec (stretch goal) and isn't implemented — worth a separate,
  deliberate design pass (thresholds, sustained-vs-spike detection) rather
  than bolting on quickly.

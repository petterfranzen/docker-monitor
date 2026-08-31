# docker-monitor

A small, standalone service that watches Docker containers on a host and
pushes an [ntfy](https://ntfy.sh) notification when one breaks. Built for a
home NAS running several independent `docker compose` stacks (this dev
machine's [flight-tracker](../flight-tracker) is one of them) — nobody
currently finds out when a container silently crashes, restart-loops,
fails its own healthcheck, or goes quiet without actually stopping. This
watches *any* container on the host by default; it has no special
knowledge of flight-tracker or any other specific stack.

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

**Not in scope for v1** (see "Decisions for the human" below): CPU/memory
resource-threshold alerting, a UI/dashboard, non-ntfy notification
channels, and pausing alerts during a planned maintenance window.

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
- This is a background poll loop with no HTTP surface of its own, so no
  web framework. A plain `while True: ... ; time.sleep(...)` loop is
  simpler and easier to reason about than pulling in a scheduler library
  for one job. Log tailing is a periodic incremental fetch
  (`since=<last checkpoint>`) each poll cycle rather than a persistent
  streaming connection, for the same reason — it fits the existing
  poll-loop model and avoids a background thread + reconnect/backoff per
  container.
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
# Leave NOTIFY_MODE=console to start — no ntfy server needed yet.
docker compose up -d --build
docker compose logs -f docker-monitor
```

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

19 tests: the container-state de-dup state machine (`test_rules.py`), the
log-content watcher's three checks — pattern matching, no-data, traffic
spike (`test_logwatch.py`, using a `FakeContainer` stub, no real Docker
daemon needed) — and the ntfy severity→priority/tag mapping
(`test_notifier.py`).

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
- **The socket mount itself.** `/var/run/docker.sock:/var/run/docker.sock:ro`
  is the same on the NAS as here — it's host-Docker-daemon-specific, not
  per-stack, so this one `docker-monitor` container watches every stack on
  the NAS at once. You don't need one instance per stack.

### Security notes

- The Docker socket is mounted **read-only** in the bind-mount sense
  (`:ro`) — but note this only stops the *bind mount itself* from being
  remounted read-write from inside the container; the Docker Engine API
  reachable over that socket is a full read/write control-plane API
  (start/stop/exec/create/delete anything on the host). This tool only
  ever calls the read endpoints (`list`, `inspect`, `logs`), but nothing at
  the socket level *enforces* that — it's enforced by this codebase not
  calling anything else. Anyone who can exec into this container has the
  same host-level power any container with socket access does.
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
  logwatch.py     # log-content watcher: no-data, pattern matching, traffic-spike (pure logic + Engine API log fetch)
  notifier.py     # console dry-run notifier + real ntfy notifier, severity -> priority/tags
  main.py         # poll loop entry point, wires state engine + log watcher + notifier together
tests/
  test_rules.py    # unit tests for rules.py
  test_logwatch.py # unit tests for logwatch.py (FakeContainer stub, no real Docker daemon)
  test_notifier.py # unit tests for the priority/tag mapping
Dockerfile
docker-compose.yml   # bundles docker-monitor + a self-hosted ntfy service
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
- **ntfy topic access control is documented, not configured.** This
  project doesn't decide LAN-only vs. internet-reachable for you — see
  "ntfy setup → Security notes."
- **CPU/memory resource-threshold alerting** was explicitly out of scope
  per the spec (stretch goal) and isn't implemented — worth a separate,
  deliberate design pass (thresholds, sustained-vs-spike detection) rather
  than bolting on quickly.

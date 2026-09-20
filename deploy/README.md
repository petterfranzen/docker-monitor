# Deploying to the UGREEN NAS

> **Deploying the whole Lab (monitor + dashboard) at once?** Use
> [`lab-stack.yml`](lab-stack.yml) instead of `docker-compose.yml`. It's
> one Compose project containing the socket proxy, ntfy, docker-monitor
> and the portfolio site, with the project registry inlined — built to be
> pasted straight into UGOS's "Create project" box, with nothing to upload
> or edit on the NAS afterwards. Read its header before deploying; there
> are three things to set.
>
> `docker-compose.yml` below remains the right file for an
> alerting-and-control instance on its own, without the dashboard.


The image is built once in CI and pulled by the NAS — nothing gets built
on the NAS itself.

## 1. Publish the image

Push to `main` (or run the workflow manually from the Actions tab). This
builds `docker-monitor` for both amd64 and arm64 and pushes it to GHCR as
`ghcr.io/petterfranzen/docker-monitor:latest`.

## 2. Make the GHCR package pullable from the NAS

GHCR packages are private by default. Pick one, on github.com (not
something I can do on your behalf):

- **Make it public** — on the package's page
  (github.com/users/petterfranzen/packages), Package settings → Change
  visibility → Public. Simplest if you're fine with the image being public
  (the source already is).
- **Keep it private** — on the NAS, `docker login ghcr.io` with a GitHub
  Personal Access Token that has `read:packages` scope. Create the token
  yourself at github.com/settings/tokens; don't paste it to me.

## 3. Get it running

`deploy/docker-compose.yml` is self-contained — every setting is inlined as
`${VAR:-default}`, the same way flight-tracker's own `deploy/` compose file
is, so the file itself is enough. No separate `.env` upload needed.

**Using a GUI Docker app** (UGOS's Docker app, Portainer, Synology
Container Manager, etc.) — look for "Project"/"Compose" (not plain
"Image"/"Container" creation, which doesn't take a multi-service compose
file):

1. Create a new project and paste in the contents of `deploy/docker-compose.yml`
   (or upload the file, if your app takes a file instead of pasted text).
2. Most of these apps scan the pasted YAML for `${...}` references and
   generate a fill-in form from them (this is exactly what flight-tracker's
   own deploy compose relies on) — that's where to set `NOTIFY_MODE=ntfy`
   and `NTFY_TOPIC` once you're ready for real pushes, rather than
   `console`. If your app doesn't do this, just edit the `:-default` value
   directly in the pasted YAML for whatever you want to change.
3. Deploy/build the project — this pulls both images and starts them.

**Over SSH**, same file, no GUI:

```bash
cd ~/docker-monitor
docker compose pull
docker compose up -d
```

(A real `.env` file dropped next to `docker-compose.yml` also works here —
docker compose reads it automatically and it overrides the `:-defaults` —
if you'd rather manage config that way than edit the compose file's
defaults directly. See `deploy/.env.example` for the full list of names.)

There's no web UI to open — docker-monitor has none, it only ever pushes
alerts out. If `NOTIFY_MODE=ntfy`, subscribe from your phone or a browser
at `http://<nas-ip>:${NTFY_PORT:-8080}` (the port the bundled `ntfy`
service publishes) to actually receive them; leave `NOTIFY_MODE=console`
and check the `docker-monitor` container's logs (via the GUI's Logs tab,
or `docker compose logs -f docker-monitor` over SSH) if you just want to
confirm it's picking up the NAS's containers first.

## Updating

```bash
docker compose pull
docker compose up -d
```

pulls whatever's currently tagged `latest` (or `IMAGE_TAG` in `.env`, if
pinned to a specific build) and recreates the containers.

## About this stack

Two containers, same as the repo-root dev compose file:

- **`docker-monitor`** mounts the host's Docker socket read-only
  (`/var/run/docker.sock:/var/run/docker.sock:ro`) — the standard pattern
  used by cAdvisor/diun/what's-up-docker/watchtower for exactly this
  reason: it needs to *observe* container state (list, inspect, read logs)
  but should never be able to build, exec into, start/stop, or otherwise
  mutate anything on the host. `cap_drop: [ALL]` and
  `security_opt: [no-new-privileges:true]` further reduce what a
  compromised process here could do beyond the socket itself — see the
  root README.md's "Security notes" for the full caveats (the `:ro` mount
  only protects the bind mount itself; the Docker Engine API reachable
  over that socket is still a full read/write control plane, enforced
  here only by this codebase never calling anything but the read
  endpoints).
- **`ntfy`** (`binwiederhier/ntfy`) is a self-hosted push notification
  server, bundled here so `NOTIFY_MODE=ntfy` works out of the box. Already
  run an ntfy server elsewhere on your NAS? Point `NTFY_URL` at that
  instead and drop this service — one ntfy server is plenty for a whole
  NAS.

What `docker-monitor` actually watches for, end to end: containers that
stop unexpectedly, restart-loop, fail their own `HEALTHCHECK`, go silent
(no new log lines for longer than `NO_DATA_IDLE_SECONDS`), start logging
known trouble text (rate-limiting, optionally generic errors/exceptions),
or spike in log volume against their own rolling baseline. One
`docker-monitor` container watches every other container on the host at
once — you don't need one instance per stack. See the root README.md for
the full config reference and the reasoning behind each default.

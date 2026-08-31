# Deploying to the UGREEN NAS

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

## 3. Get the compose files onto the NAS

Copy `deploy/docker-compose.yml` and a filled-in copy of `deploy/.env.example`
(as `.env`, same directory) onto the NAS — e.g. via UGOS's File Manager, or
`scp deploy/docker-compose.yml deploy/.env <nas-user>@<nas-host>:~/docker-monitor/`.

## 4. Run it

Either through UGOS's Docker app (Container Manager-style UI: create a new
Compose project, point it at the uploaded `docker-compose.yml`), or over SSH:

```bash
cd ~/docker-monitor
docker compose pull
docker compose up -d
```

There's no web UI to open — docker-monitor has none, it only ever pushes
alerts out. If `NOTIFY_MODE=ntfy`, subscribe from your phone or a browser
at `http://<nas-ip>:${NTFY_PORT:-8080}` (the port the bundled `ntfy`
service publishes) to actually receive them; leave `NOTIFY_MODE=console`
and check `docker compose logs -f docker-monitor` if you just want to
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

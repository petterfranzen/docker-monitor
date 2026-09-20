# Two things live in this image: the monitor itself, and the `docker
# compose` CLI it drives to start/stop/update projects.
#
# The compose binary is copied from Docker's own published CLI image
# rather than installed from apt — apt would mean adding Docker's
# repository, a GPG key and ~200MB of packages to get one plugin binary.
# buildx is deliberately left behind: nothing here ever builds an image
# (projects run from prebuilt images pulled from GHCR), and it's the
# largest of the two plugins.
FROM docker:28-cli AS dockercli

FROM python:3.12-slim

WORKDIR /app

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose \
     /usr/local/libexec/docker/cli-plugins/docker-compose

# ntfy pushes go over Python's own urllib.request (no extra dependency for
# that). The rest: `docker` is the Engine API client, and fastapi/uvicorn
# serve the control API — see docker_monitor/api.py for why that one is
# worth a dependency and http.server isn't.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker_monitor/ ./docker_monitor/

# Runs as root: reading the Docker socket needs root or membership in the
# host's "docker" group, and that group's GID varies per host/distro so it
# can't be baked into the image generically. On the NAS this doesn't talk
# to the socket at all — it talks to the socket proxy over TCP (see
# deploy/docker-compose.yml), which is the better answer to this. The
# blast radius is otherwise contained at the compose level: all Linux
# capabilities dropped, no-new-privileges, and only the API port
# published. See README.md's "Security notes".
CMD ["python", "-m", "docker_monitor.main"]

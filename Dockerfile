# Small, single-purpose image: Python's own smtplib covers SMTP (no extra
# dependency for that), so `docker` — the Engine API client — is the only
# third-party package this needs. No web framework, no build step.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker_monitor/ ./docker_monitor/

# Runs as root: reading /var/run/docker.sock needs root or membership in
# the host's "docker" group, and that group's GID varies per host/distro so
# it can't be baked into the image generically. The blast radius is
# contained at the docker-compose.yml level instead — read-only socket
# mount, all Linux capabilities dropped, no-new-privileges, no published
# ports — rather than by dropping root inside this container. See
# README.md's "Security notes" for the docker-socket-proxy alternative if
# you'd rather avoid a root process entirely.
CMD ["python", "-m", "docker_monitor.main"]

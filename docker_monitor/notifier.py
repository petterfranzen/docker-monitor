"""Turns Events into an outgoing notification: either a real push via a
self-hosted ntfy (https://ntfy.sh) server — a single HTTP POST, no client
library needed — or a console dry-run that logs exactly what would have
been sent (including the priority/tags a real push would carry), so the
whole detection pipeline can be exercised without a reachable ntfy server.
"""
from __future__ import annotations

import logging
import socket
import urllib.error
import urllib.request

logger = logging.getLogger("docker_monitor")


def _format(event) -> tuple:
    host = socket.gethostname()
    status = "recovered" if event.recovered else "problem detected"
    tag_word = "RECOVERED" if event.recovered else "PROBLEM"
    subject = f"[docker-monitor] {tag_word}: {event.container_name} ({event.alert_type})"
    body = (
        f"Host: {host}\n"
        f"Container: {event.container_name}\n"
        f"Alert type: {event.alert_type}\n"
        f"Severity: {event.severity}\n"
        f"Status: {status}\n"
        f"Details: {event.detail}\n"
    )
    return subject, body


def _priority_and_tags(event) -> tuple:
    """ntfy priority (1-5, 5=urgent) and emoji-shortcode tags. Container-state
    problems (severity="critical": stopped/unhealthy/restart-loop) are high
    priority; log-content problems (severity="warning": rate-limited,
    no-data, traffic-spike) are lower — so a glance at the phone's
    notification (icon + priority) tells urgency apart without opening it.
    Recovery notifications are always lower priority than the problem they
    clear, regardless of original severity."""
    if event.recovered:
        return (3, ["white_check_mark"]) if event.severity == "critical" else (2, ["white_check_mark"])
    if event.severity == "critical":
        return (5, ["rotating_light"])
    return (3, ["warning"])


class ConsoleNotifier:
    """Dry-run notifier: logs what would have been pushed instead of
    sending anything. This is the default (NOTIFY_MODE=console) so the tool
    is safe and useful to try out before a real ntfy server/topic exists."""

    def send(self, event) -> None:
        subject, body = _format(event)
        priority, tags = _priority_and_tags(event)
        logger.info(
            "DRY-RUN NTFY PUSH WOULD BE SENT (priority=%s, tags=%s)\nTitle: %s\n%s",
            priority,
            ",".join(tags),
            subject,
            body,
        )


class NtfyNotifier:
    """Publishes to a topic on an ntfy server via a plain HTTP POST — see
    https://docs.ntfy.sh/publish/. Uses urllib (stdlib) rather than adding
    a requests dependency of our own for one call site."""

    def __init__(self, cfg):
        self._cfg = cfg

    def send(self, event) -> None:
        subject, body = _format(event)
        priority, tags = _priority_and_tags(event)
        url = f"{self._cfg.ntfy_url.rstrip('/')}/{self._cfg.ntfy_topic}"

        # Title/Tags/Priority go in headers per ntfy's publish API; body is
        # the message text. Container names, alert types, and our own
        # formatted text are always plain ASCII, so no header encoding
        # concerns here — a non-ASCII title would need RFC 2047 encoding,
        # which ntfy also supports but isn't needed for anything this tool
        # generates.
        request = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers={
                "Title": subject,
                "Priority": str(priority),
                "Tags": ",".join(tags),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
            logger.info("Sent ntfy push: %s (priority=%s)", subject, priority)
        except (urllib.error.URLError, OSError):
            logger.exception("Failed to send ntfy push for %s", subject)


def build_notifier(cfg):
    if cfg.notify_mode == "ntfy":
        return NtfyNotifier(cfg)
    return ConsoleNotifier()

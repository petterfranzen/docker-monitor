"""Turns Events into an outgoing notification: either a real email (SMTP,
stdlib smtplib — no extra dependency) or a console dry-run that logs
exactly what would have been sent, so the whole detection pipeline can be
exercised end-to-end without real SMTP credentials.
"""
from __future__ import annotations

import logging
import smtplib
import socket
from email.message import EmailMessage

logger = logging.getLogger("docker_monitor")


def _format(event) -> tuple:
    host = socket.gethostname()
    status = "recovered" if event.recovered else "problem detected"
    tag = "RECOVERED" if event.recovered else "PROBLEM"
    subject = f"[docker-monitor] {tag}: {event.container_name} ({event.alert_type})"
    body = (
        f"Host: {host}\n"
        f"Container: {event.container_name}\n"
        f"Alert type: {event.alert_type}\n"
        f"Status: {status}\n"
        f"Details: {event.detail}\n"
    )
    return subject, body


class ConsoleNotifier:
    """Dry-run notifier: logs what would have been emailed instead of
    sending anything. This is the default (NOTIFY_MODE=console) so the tool
    is safe and useful to try out before real SMTP credentials exist."""

    def send(self, event) -> None:
        subject, body = _format(event)
        logger.info("DRY-RUN EMAIL WOULD BE SENT\nSubject: %s\n%s", subject, body)


class EmailNotifier:
    def __init__(self, cfg):
        self._cfg = cfg

    def send(self, event) -> None:
        subject, body = _format(event)
        cfg = self._cfg

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.smtp_from
        msg["To"] = ", ".join(cfg.smtp_to)
        msg.set_content(body)

        if cfg.smtp_tls_mode == "ssl":
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)

        try:
            if cfg.smtp_tls_mode == "starttls":
                server.starttls()
            if cfg.smtp_username:
                server.login(cfg.smtp_username, cfg.smtp_password)
            server.send_message(msg)
            logger.info("Sent email: %s", subject)
        finally:
            server.quit()


def build_notifier(cfg):
    if cfg.notify_mode == "email":
        return EmailNotifier(cfg)
    return ConsoleNotifier()

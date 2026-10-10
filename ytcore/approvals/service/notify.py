"""Operator notifications: items waiting, deadlines approaching, kill switch used.

Notifier implementations: Fake (default), Slack incoming webhook, SMTP email. Webhook URLs and
SMTP credentials are secret references resolved through the SecretsProvider.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import TYPE_CHECKING, Literal, Protocol

import httpx

from ytcore.secrets.provider import SecretRef, SecretsProvider

if TYPE_CHECKING:
    from ytcore.approvals.service.repo import HubRepository
    from ytcore.approvals.service.settings import HubSettings

log = logging.getLogger(__name__)
Kind = Literal["items_waiting", "deadline_approaching", "kill_switch", "breaker_tripped"]


@dataclass(frozen=True)
class Notification:
    kind: Kind
    title: str
    body: str
    link: str = ""


class Notifier(Protocol):
    def send(self, note: Notification) -> None: ...


@dataclass
class FakeNotifier:
    sent: list[Notification] = field(default_factory=list)

    def send(self, note: Notification) -> None:
        self.sent.append(note)


class SlackNotifier:
    def __init__(self, secrets: SecretsProvider, webhook_ref: str) -> None:
        self._secrets = secrets
        self._ref = SecretRef(webhook_ref)

    def send(self, note: Notification) -> None:
        text = f"*{note.title}*\n{note.body}" + (f"\n<{note.link}|Open hub>" if note.link else "")
        url = self._secrets.get(self._ref).reveal()
        resp = httpx.post(url, json={"text": text}, timeout=10)
        if resp.status_code >= 300:
            log.warning("slack notification failed with status %s", resp.status_code)


class EmailNotifier:
    def __init__(self, secrets: SecretsProvider, settings: HubSettings) -> None:
        self._secrets = secrets
        self._s = settings

    def send(self, note: Notification) -> None:
        if not self._s.email_to:
            return
        msg = EmailMessage()
        msg["Subject"] = f"[yt-agentic] {note.title}"
        msg["From"] = self._s.email_from
        msg["To"] = ", ".join(self._s.email_to)
        msg.set_content(note.body + (f"\n\n{note.link}" if note.link else ""))
        with smtplib.SMTP(self._s.smtp_host, self._s.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(
                self._secrets.get(SecretRef(self._s.smtp_user_ref)).reveal(),
                self._secrets.get(SecretRef(self._s.smtp_password_ref)).reveal(),
            )
            smtp.send_message(msg)


def build_notifier(settings: HubSettings, secrets: SecretsProvider) -> Notifier:
    if settings.notifier_backend == "slack":
        return SlackNotifier(secrets, settings.slack_webhook_ref)
    if settings.notifier_backend == "email":
        return EmailNotifier(secrets, settings)
    return FakeNotifier()


def notify_pending(
    repo: HubRepository,
    notifier: Notifier,
    *,
    now: datetime,
    deadline_window: timedelta = timedelta(hours=6),
    link: str = "",
) -> list[Notification]:
    """Send at most one note per waiting batch and per approaching deadline (deduplicated)."""
    sent: list[Notification] = []
    waiting = repo.waiting_summary()
    if waiting.count and repo.record_notification("items_waiting", waiting.dedup_key):
        note = Notification(
            "items_waiting",
            f"{waiting.count} video(s) waiting for approval",
            f"Oldest waiting since {waiting.oldest:%Y-%m-%d %H:%M} UTC." if waiting.oldest else "",
            link,
        )
        notifier.send(note)
        sent.append(note)
    for job_id, title, deadline in repo.deadlines_before(now + deadline_window):
        if repo.record_notification("deadline_approaching", job_id):
            note = Notification(
                "deadline_approaching",
                f"Approval deadline approaching: {title}",
                f"Job {job_id} is cancelled and re-planned if not approved by "
                f"{deadline:%Y-%m-%d %H:%M} UTC.",
                f"{link}/jobs/{job_id}" if link else "",
            )
            notifier.send(note)
            sent.append(note)
    return sent

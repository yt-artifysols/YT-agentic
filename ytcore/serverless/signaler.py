"""The hub's decisions to the job driver through the database (replaces Temporal signals).

The decision row carries the approval token the hub already consumed; the driver erases it
when it consumes the decision. Nothing here can approve anything: the approval itself was
recorded by the hub in its own transaction before this row is written."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import psycopg

from ytcore.workflow.models import HubDecision

log = logging.getLogger(__name__)


class DbSignaler:
    """`wake_url` (optional) is the job driver's cron endpoint: poked after a decision so the job
    moves at once instead of on the next scheduled run. Best effort; the cron run is the
    guarantee, and the poke carries no data."""

    def __init__(
        self, conninfo: str, *, wake_url: str = "", wake_secret: str = "", wake_timeout: float = 3.0
    ) -> None:
        self._conninfo = conninfo
        self._wake_url = wake_url
        self._wake_secret = wake_secret
        self._wake_timeout = wake_timeout

    def _wake(self) -> None:
        if not self._wake_url:
            return
        try:
            httpx.get(
                self._wake_url,
                headers={"Authorization": f"Bearer {self._wake_secret}"},
                timeout=self._wake_timeout,
            )
        except httpx.HTTPError:  # the next scheduled run picks the decision up
            log.info("could not wake the job driver; the next scheduled run will")

    def _insert(self, job_id: str, d: HubDecision) -> None:
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            _insert(conn, job_id, d)

    async def send(self, job_id: str, decision: HubDecision) -> None:
        await asyncio.to_thread(self._insert, job_id, decision)
        await asyncio.to_thread(self._wake)


def _insert(conn: psycopg.Connection[Any], job_id: str, d: HubDecision) -> None:
    conn.execute(
        "INSERT INTO workflow_decisions "
        "(job_id, action, actor, content_hash, token, rework_stage, reason) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (job_id, d.action, d.actor, d.content_hash, d.token, d.rework_stage, d.reason),
    )

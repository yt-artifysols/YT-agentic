"""WebAuthn ceremonies and step-up tickets in Postgres, so any hub instance can finish a
ceremony another instance started. Same interface as the in-process CeremonyStore."""

from __future__ import annotations

import secrets
import time
from typing import Any

import psycopg
from fastapi import HTTPException

from ytcore.approvals.service.security import Ceremony


class DbCeremonyStore:
    TTL = 300.0

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self._conninfo, autocommit=True)

    def put(self, ceremony: Ceremony) -> str:
        key = secrets.token_urlsafe(32)
        ttl = max(0.0, ceremony.expires - time.monotonic())
        with self._connect() as conn:
            conn.execute("DELETE FROM hub_ceremonies WHERE expires_at < now()")
            conn.execute(
                "INSERT INTO hub_ceremonies "
                "(key, kind, challenge, expires_at, user_id, invite_token, subject) "
                "VALUES (%s, %s, %s, now() + make_interval(secs => %s), %s, %s, %s)",
                (
                    key,
                    ceremony.kind,
                    ceremony.challenge,
                    ttl,
                    ceremony.user_id,
                    ceremony.invite_token,
                    ceremony.subject,
                ),
            )
        return key

    def pop(self, key: str | None, kind: str) -> Ceremony:
        row = None
        if key:
            with self._connect() as conn:
                # One statement: a ceremony can be completed at most once, by whoever gets it.
                row = conn.execute(
                    "DELETE FROM hub_ceremonies WHERE key = %s AND expires_at > now() "
                    "RETURNING kind, challenge, extract(epoch FROM expires_at - now()), "
                    "user_id, invite_token, subject",
                    (key,),
                ).fetchone()
        if row is None or row[0] != kind:
            raise HTTPException(400, "no active passkey ceremony; start again")
        return Ceremony(
            bytes(row[1]),
            row[0],
            time.monotonic() + float(row[2]),
            user_id=str(row[3]) if row[3] else None,
            invite_token=row[4],
            subject=row[5],
        )

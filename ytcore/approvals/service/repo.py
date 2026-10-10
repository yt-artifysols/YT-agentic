"""Postgres repository for the approval hub. DB triggers stay the enforcement layer."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from ytcore.approvals.hashing import content_hash
from ytcore.store.errors import StoreError
from ytcore.store.postgres import Connect, map_db_error

Role = Literal["approver", "operator", "admin"]


def sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


@dataclass(frozen=True)
class QueueItem:
    job_id: str
    channel_id: str
    cluster_id: str | None
    title: str
    origin: str
    content_hash: str
    manifest_version: int
    risk_score: int | None
    qa_passed: bool | None
    flag_count: int
    approval_deadline: datetime | None
    fast_lane: bool


@dataclass(frozen=True)
class JobHead:
    job_id: str
    channel_id: str
    state: str
    content_hash: str | None
    manifest: dict[str, Any] | None
    manifest_version: int
    storage_prefix: str


@dataclass(frozen=True)
class QAInfo:
    passed: bool
    risk_score: int
    flags: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    content_hash: str | None


@dataclass(frozen=True)
class User:
    id: str
    username: str
    display_name: str
    role: Role
    disabled: bool


@dataclass(frozen=True)
class Credential:
    credential_id: bytes
    user_id: str
    public_key: bytes
    sign_count: int
    transports: list[str]


@dataclass(frozen=True)
class Session:
    user: User
    csrf_hash: bytes
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class WaitingSummary:
    count: int
    oldest: datetime | None

    @property
    def dedup_key(self) -> str:
        return f"{self.count}@{self.oldest.isoformat() if self.oldest else '-'}"


class HubConflict(StoreError):
    """The job changed since the reviewer loaded it (stale preview) or is not awaiting approval."""


class HubRepository:
    def __init__(self, connect: Connect, *, fast_lane_max_risk: int = 20) -> None:
        self._connect = connect
        self._fast_lane_max_risk = fast_lane_max_risk

    @classmethod
    def from_conninfo(cls, conninfo: str, **kw: Any) -> HubRepository:
        return cls(lambda: psycopg.connect(conninfo, autocommit=True), **kw)

    @contextmanager
    def _tx(self, actor: str) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (actor,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    @staticmethod
    def _one(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...] = ()) -> Any:
        return conn.execute(sql, params).fetchone()

    @staticmethod
    def _all(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...] = ()) -> Any:
        return conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ audit

    def audit(self, actor: str, action: str, subject: str, payload: dict[str, Any]) -> None:
        with self._tx(actor) as conn:
            conn.execute("SELECT audit(%s, %s, %s)", (action, subject, Jsonb(payload)))

    # ------------------------------------------------------------------ queue and preview

    _LATEST_QA = """
        LEFT JOIN LATERAL (
            SELECT passed, risk_score, flags, sources, content_hash FROM qa_reports q
            WHERE q.job_id = j.id ORDER BY q.id DESC LIMIT 1
        ) qa ON true
    """

    def queue(
        self,
        *,
        cluster: str | None = None,
        channel: str | None = None,
        min_risk: int | None = None,
        max_risk: int | None = None,
        fast_lane_only: bool = False,
    ) -> list[QueueItem]:
        sql = (
            "SELECT j.id::text, j.channel_id, c.cluster_id, "
            "coalesce(j.manifest->'metadata'->>'title', ''), j.origin, j.content_hash, "
            "j.manifest_version, qa.risk_score, qa.passed, "
            "coalesce(jsonb_array_length(qa.flags), 0), j.approval_deadline "
            "FROM video_jobs j JOIN channels c ON c.id = j.channel_id "
            + self._LATEST_QA
            + " WHERE j.state = 'awaiting_approval'"
            " AND (%(cluster)s::text IS NULL OR c.cluster_id = %(cluster)s)"
            " AND (%(channel)s::text IS NULL OR j.channel_id = %(channel)s)"
            " AND (%(min)s::int IS NULL OR coalesce(qa.risk_score, 100) >= %(min)s)"
            " AND (%(max)s::int IS NULL OR coalesce(qa.risk_score, 100) <= %(max)s)"
            " ORDER BY coalesce(qa.risk_score, 100) DESC, j.approval_deadline ASC NULLS LAST"
        )
        params = {"cluster": cluster, "channel": channel, "min": min_risk, "max": max_risk}
        with self._tx("hub:reader") as conn:
            rows = conn.execute(sql, params).fetchall()
        items = [
            QueueItem(
                job_id=r[0],
                channel_id=r[1],
                cluster_id=r[2],
                title=r[3],
                origin=r[4],
                content_hash=r[5],
                manifest_version=r[6],
                risk_score=r[7],
                qa_passed=r[8],
                flag_count=r[9],
                approval_deadline=r[10],
                fast_lane=self._is_fast_lane(r[8], r[7], r[9]),
            )
            for r in rows
        ]
        return [i for i in items if i.fast_lane] if fast_lane_only else items

    def _is_fast_lane(self, passed: bool | None, risk: int | None, flags: int) -> bool:
        return bool(passed) and risk is not None and risk <= self._fast_lane_max_risk and not flags

    def head(self, job_id: str) -> JobHead | None:
        with self._tx("hub:reader") as conn:
            r = self._one(
                conn,
                "SELECT j.id::text, j.channel_id, j.state, j.content_hash, j.manifest, "
                "j.manifest_version, c.storage_prefix FROM video_jobs j "
                "JOIN channels c ON c.id = j.channel_id WHERE j.id = %s",
                (job_id,),
            )
        return None if r is None else JobHead(*r)

    def qa(self, job_id: str) -> QAInfo | None:
        with self._tx("hub:reader") as conn:
            r = self._one(
                conn,
                "SELECT passed, risk_score, flags, sources, content_hash FROM qa_reports "
                "WHERE job_id = %s ORDER BY id DESC LIMIT 1",
                (job_id,),
            )
        return None if r is None else QAInfo(*r)

    def manifest_version(self, job_id: str, version: int) -> dict[str, Any] | None:
        with self._tx("hub:reader") as conn:
            r = self._one(
                conn,
                "SELECT manifest FROM manifest_versions WHERE job_id = %s AND version = %s",
                (job_id, version),
            )
        return None if r is None else dict(r[0])

    def is_fast_lane(self, job_id: str) -> bool:
        """Fast lane needs a passing, low-risk, unflagged QA report for the CURRENT content."""
        info, head = self.qa(job_id), self.head(job_id)
        if info is None or head is None:
            return False
        if info.content_hash not in (None, head.content_hash):
            return False
        return self._is_fast_lane(info.passed, info.risk_score, len(info.flags))

    # ------------------------------------------------------------------ decisions

    def apply_edit(
        self, job_id: str, expected_hash: str, changes: dict[str, Any], *, actor: str
    ) -> tuple[str, int]:
        """Store an edit as a new manifest version and send the job back to QA."""
        with self._tx(actor) as conn:
            r = self._one(
                conn,
                "SELECT state, content_hash, manifest FROM video_jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            )
            if r is None:
                raise HubConflict("job not found")
            state, current, manifest = r
            if state != "awaiting_approval" or current != expected_hash:
                raise HubConflict("content changed since the preview was loaded")
            new_manifest = {**manifest, "metadata": {**manifest.get("metadata", {}), **changes}}
            new_hash = content_hash(new_manifest)
            if new_hash == current:
                raise HubConflict("edit does not change the content")
            conn.execute("UPDATE video_jobs SET state = 'qa' WHERE id = %s", (job_id,))
            v = self._one(
                conn,
                "UPDATE video_jobs SET manifest = %s, content_hash = %s WHERE id = %s "
                "RETURNING manifest_version",
                (Jsonb(new_manifest), new_hash, job_id),
            )
            conn.execute(
                "SELECT audit('hub.edit', %s, %s)",
                (
                    f"job:{job_id}",
                    Jsonb({"from": current, "to": new_hash, "fields": sorted(changes)}),
                ),
            )
        return new_hash, int(v[0])

    def record_rejection(
        self, job_id: str, expected_hash: str, reason: str, stage: str, *, username: str
    ) -> None:
        with self._tx(f"hub:{username}") as conn:
            r = self._one(conn, "SELECT channel_id FROM video_jobs WHERE id = %s", (job_id,))
            if r is None:
                raise HubConflict("job not found")
            try:
                conn.execute(
                    "INSERT INTO job_rejections (job_id, channel_id, content_hash, reason, "
                    "rework_stage, rejected_by) VALUES (%s, %s, %s, %s, %s, %s)",
                    (job_id, r[0], expected_hash, reason, stage, username),
                )
            except psycopg.errors.CheckViolation as exc:
                raise HubConflict(exc.diag.message_primary or "rejection refused") from exc

    # ------------------------------------------------------------------ kill switches

    def controls(self) -> dict[str, Any]:
        with self._tx("hub:reader") as conn:
            paused = self._one(conn, "SELECT publishing_paused FROM global_controls")[0]
            clusters = self._all(conn, "SELECT id, display_name, status FROM clusters ORDER BY id")
            channels = self._all(
                conn,
                "SELECT id, display_name, cluster_id, status, autonomy_tier FROM channels "
                "ORDER BY id",
            )
        return {
            "global_paused": paused,
            "clusters": [{"id": c[0], "name": c[1], "status": c[2]} for c in clusters],
            "channels": [
                {"id": c[0], "name": c[1], "cluster_id": c[2], "status": c[3], "tier": c[4]}
                for c in channels
            ],
        }

    def set_global_pause(self, paused: bool, *, actor: str) -> None:
        with self._tx(actor) as conn:
            conn.execute("UPDATE global_controls SET publishing_paused = %s", (paused,))

    def set_status(self, table: Literal["clusters", "channels"], ident: str, paused: bool, *,
                   actor: str) -> bool:  # fmt: skip
        status = "paused" if paused else "active"
        sql = {
            "clusters": "UPDATE clusters SET status = %s WHERE id = %s",
            "channels": "UPDATE channels SET status = %s WHERE id = %s",
        }[table]
        with self._tx(actor) as conn:
            return conn.execute(sql, (status, ident)).rowcount == 1

    # ------------------------------------------------------------------ users and auth

    _USER_COLS = "id::text, username, display_name, role, disabled_at IS NOT NULL"

    def create_user(self, username: str, display_name: str, role: Role, *, actor: str) -> User:
        with self._tx(actor) as conn:
            r = self._one(
                conn,
                "INSERT INTO hub_users (username, display_name, role) VALUES (%s, %s, %s) "  # noqa: S608
                f"RETURNING {self._USER_COLS}",
                (username, display_name, role),
            )
        return User(*r)

    def user_by_name(self, username: str) -> User | None:
        with self._tx("hub:auth") as conn:
            r = self._one(
                conn,
                f"SELECT {self._USER_COLS} FROM hub_users WHERE username = %s",  # noqa: S608
                (username,),
            )
        return None if r is None else User(*r)

    def user_by_id(self, user_id: str) -> User | None:
        with self._tx("hub:auth") as conn:
            r = self._one(
                conn,
                f"SELECT {self._USER_COLS} FROM hub_users WHERE id = %s",  # noqa: S608
                (user_id,),
            )
        return None if r is None else User(*r)

    def create_invite(self, user_id: str, token: str, expires_at: datetime, *, actor: str) -> None:
        with self._tx(actor) as conn:
            conn.execute(
                "INSERT INTO hub_invites (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                (sha256(token), user_id, expires_at),
            )
            conn.execute(
                "SELECT audit('hub.invite.created', 'user:' || username, '{}'::jsonb) "
                "FROM hub_users WHERE id = %s",
                (user_id,),
            )

    def invite_user(self, token: str) -> User | None:
        """The user an unused, unexpired invite belongs to (does not consume it)."""
        with self._tx("hub:auth") as conn:
            r = self._one(
                conn,
                "SELECT u.id::text, u.username, u.display_name, u.role, u.disabled_at IS NOT NULL "
                "FROM hub_invites i JOIN hub_users u ON u.id = i.user_id "
                "WHERE i.token_hash = %s AND i.used_at IS NULL AND i.expires_at > now()",
                (sha256(token),),
            )
        return None if r is None else User(*r)

    def register_credential(self, invite_token: str, cred: Credential, *, actor: str) -> bool:
        """Consume the invite and store the credential atomically."""
        with self._tx(actor) as conn:
            used = conn.execute(
                "UPDATE hub_invites SET used_at = now() WHERE token_hash = %s AND used_at IS NULL "
                "AND expires_at > now() AND user_id = %s",
                (sha256(invite_token), cred.user_id),
            ).rowcount
            if used != 1:
                return False
            conn.execute(
                "INSERT INTO webauthn_credentials (credential_id, user_id, public_key, sign_count,"
                " transports) VALUES (%s, %s, %s, %s, %s)",
                (cred.credential_id, cred.user_id, cred.public_key, cred.sign_count,
                 cred.transports),
            )  # fmt: skip
            conn.execute(
                "SELECT audit('hub.passkey.registered', 'user:' || username, '{}'::jsonb) "
                "FROM hub_users WHERE id = %s",
                (cred.user_id,),
            )
        return True

    def credentials_for(self, user_id: str) -> list[Credential]:
        with self._tx("hub:auth") as conn:
            rows = self._all(
                conn,
                "SELECT credential_id, user_id::text, public_key, sign_count, transports "
                "FROM webauthn_credentials WHERE user_id = %s",
                (user_id,),
            )
        return [Credential(bytes(r[0]), r[1], bytes(r[2]), r[3], list(r[4])) for r in rows]

    def credential(self, credential_id: bytes) -> Credential | None:
        with self._tx("hub:auth") as conn:
            r = self._one(
                conn,
                "SELECT credential_id, user_id::text, public_key, sign_count, transports "
                "FROM webauthn_credentials WHERE credential_id = %s",
                (credential_id,),
            )
        return None if r is None else Credential(bytes(r[0]), r[1], bytes(r[2]), r[3], list(r[4]))

    def bump_sign_count(self, credential_id: bytes, sign_count: int) -> None:
        with self._tx("hub:auth") as conn:
            conn.execute(
                "UPDATE webauthn_credentials SET sign_count = %s, last_used_at = now() "
                "WHERE credential_id = %s",
                (sign_count, credential_id),
            )

    def create_session(
        self, user_id: str, token: str, csrf: str, expires_at: datetime, *, actor: str
    ) -> None:
        with self._tx(actor) as conn:
            conn.execute(
                "INSERT INTO hub_sessions (token_hash, user_id, csrf_hash, expires_at) "
                "VALUES (%s, %s, %s, %s)",
                (sha256(token), user_id, sha256(csrf), expires_at),
            )

    def session(self, token: str) -> Session | None:
        with self._tx("hub:auth") as conn:
            r = self._one(
                conn,
                "SELECT u.id::text, u.username, u.display_name, u.role, "
                "u.disabled_at IS NOT NULL, s.csrf_hash, s.created_at, s.last_seen_at, "
                "s.expires_at FROM hub_sessions s JOIN hub_users u ON u.id = s.user_id "
                "WHERE s.token_hash = %s AND s.revoked_at IS NULL",
                (sha256(token),),
            )
        if r is None:
            return None
        return Session(User(*r[:5]), bytes(r[5]), r[6], r[7], r[8])

    def touch_session(self, token: str) -> None:
        with self._tx("hub:auth") as conn:
            conn.execute(
                "UPDATE hub_sessions SET last_seen_at = now() WHERE token_hash = %s",
                (sha256(token),),
            )

    def rotate_csrf(self, token: str, csrf: str) -> None:
        with self._tx("hub:auth") as conn:
            conn.execute(
                "UPDATE hub_sessions SET csrf_hash = %s, last_seen_at = now() "
                "WHERE token_hash = %s AND revoked_at IS NULL",
                (sha256(csrf), sha256(token)),
            )

    def revoke_session(self, token: str) -> None:
        with self._tx("hub:auth") as conn:
            conn.execute(
                "UPDATE hub_sessions SET revoked_at = now() WHERE token_hash = %s "
                "AND revoked_at IS NULL",
                (sha256(token),),
            )

    # ------------------------------------------------------------------ notifications

    def waiting_summary(self) -> WaitingSummary:
        with self._tx("hub:notifier") as conn:
            r = self._one(
                conn,
                "SELECT count(*), min(j.updated_at) FROM video_jobs j "
                "WHERE j.state = 'awaiting_approval'",
            )
        return WaitingSummary(int(r[0]), r[1])

    def deadlines_before(self, cutoff: datetime) -> list[tuple[str, str, datetime]]:
        with self._tx("hub:notifier") as conn:
            rows = self._all(
                conn,
                "SELECT id::text, coalesce(manifest->'metadata'->>'title', ''), approval_deadline "
                "FROM video_jobs WHERE state = 'awaiting_approval' "
                "AND approval_deadline IS NOT NULL AND approval_deadline <= %s",
                (cutoff,),
            )
        return [(r[0], r[1], r[2]) for r in rows]

    def record_notification(self, kind: str, dedup: str) -> bool:
        with self._tx("hub:notifier") as conn:
            return (
                conn.execute(
                    "INSERT INTO hub_notifications (kind, dedup) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (kind, dedup),
                ).rowcount
                == 1
            )

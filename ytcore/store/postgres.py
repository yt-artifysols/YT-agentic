"""Postgres-backed store (psycopg 3).

Implements the workflow Backend protocol (ytcore/workflow/activities.py) and the gateway
GatewayRepository protocol (ytcore/gateway/service.py). The database triggers are the
enforcement; this module only orchestrates single transactions and converts trigger errors into
typed exceptions (ytcore/store/errors.py).

Every write transaction first runs `set_config('app.actor', <actor>, true)` (the parameterized
form of SET LOCAL app.actor) so job_events and audit_events record who acted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from ytcore.approvals.hashing import content_hash
from ytcore.approvals.token import ApprovalClaims, ApprovalError, ApprovalVerifier
from ytcore.secrets.provider import SecretRef, SecretsProvider
from ytcore.store.errors import (
    ApprovalGateError,
    ApprovalInvalid,
    ApprovalReplayError,
    BudgetExceeded,
    ChannelNotPublishable,
    ContentFrozen,
    IllegalTransition,
    JobNotFound,
    QuotaExhausted,
    StoreError,
    UploadGuardError,
)
from ytcore.store.models import PublishContext, UploadRecord
from ytcore.workflow.states import JobState

Connect = Callable[[], psycopg.Connection[Any]]

_BY_SQLSTATE: dict[str, type[StoreError]] = {
    "YTB01": BudgetExceeded,
    "YTQ01": QuotaExhausted,
    "YTC01": ChannelNotPublishable,
    "YTU01": UploadGuardError,
}
_BY_MESSAGE: tuple[tuple[str, str, type[StoreError]], ...] = (
    ("23514", "illegal job transition", IllegalTransition),
    ("23514", "content_hash is frozen", ContentFrozen),
    ("23514", "approvals can only be recorded", ApprovalGateError),
    ("23514", "approval content_hash does not match", ApprovalGateError),
    ("42501", "approval gate", ApprovalGateError),
    ("42501", "upload requires a valid approval", ApprovalGateError),
)


def map_db_error(exc: psycopg.Error) -> StoreError:
    state = exc.sqlstate or ""
    message = exc.diag.message_primary or ""
    if state in _BY_SQLSTATE:
        return _BY_SQLSTATE[state](message)
    if isinstance(exc, pg_errors.UniqueViolation) and "token_jti" in (
        exc.diag.constraint_name or ""
    ):
        return ApprovalReplayError("approval token already used")
    for code, prefix, cls in _BY_MESSAGE:
        if state == code and message.startswith(prefix):
            return cls(message)
    return StoreError(f"{state}: {message}")


class PostgresStore:
    def __init__(self, connect: Connect, verifier: ApprovalVerifier) -> None:
        self._connect = connect
        self._verifier = verifier  # PUBLIC key only

    @classmethod
    def from_conninfo(cls, conninfo: str, verifier: ApprovalVerifier) -> PostgresStore:
        return cls(lambda: psycopg.connect(conninfo, autocommit=True), verifier)

    @classmethod
    def from_secrets(
        cls, secrets: SecretsProvider, database_url_ref: str, verifier: ApprovalVerifier
    ) -> PostgresStore:
        dsn = secrets.get(SecretRef(database_url_ref))
        return cls(lambda: psycopg.connect(dsn.reveal(), autocommit=True), verifier)

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def _tx(self, actor: str) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (actor,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    @staticmethod
    def _set_actor(conn: psycopg.Connection[Any], actor: str) -> None:
        conn.execute("SELECT set_config('app.actor', %s, true)", (actor,))

    @staticmethod
    def _one(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...]) -> Any:
        return conn.execute(sql, params).fetchone()

    # ------------------------------------------------------------------ jobs (Backend)

    def create_job(
        self, channel_id: str, origin: str, *, actor: str, parent_job_id: str | None = None
    ) -> str:
        with self._tx(actor) as conn:
            row = self._one(
                conn,
                "INSERT INTO video_jobs (channel_id, origin, parent_job_id) "
                "VALUES (%s, %s, %s) RETURNING id::text",
                (channel_id, origin, parent_job_id),
            )
        return str(row[0])

    def get_state(self, job_id: str) -> JobState:
        with self._tx("reader") as conn:
            row = self._one(conn, "SELECT state FROM video_jobs WHERE id = %s", (job_id,))
        if row is None:
            raise JobNotFound(job_id)
        return JobState(row[0])

    def transition(self, job_id: str, to_state: JobState, *, actor: str) -> None:
        with self._tx(actor) as conn:
            cur = conn.execute(
                "UPDATE video_jobs SET state = %s WHERE id = %s", (to_state.value, job_id)
            )
            if cur.rowcount != 1:
                raise JobNotFound(job_id)

    def submit_for_approval(self, job_id: str, manifest: dict[str, Any], *, actor: str) -> str:
        chash = content_hash(manifest)
        with self._tx(actor) as conn:
            cur = conn.execute(
                "UPDATE video_jobs SET manifest = %s, content_hash = %s, "
                "state = 'awaiting_approval' WHERE id = %s",
                (Jsonb(manifest), chash, job_id),
            )
            if cur.rowcount != 1:
                raise JobNotFound(job_id)
        return chash

    def verify_approval(self, job_id: str, token: str, *, actor: str) -> ApprovalClaims:
        """Verify with the PUBLIC key, then consume the jti and approve in ONE transaction."""
        with self._tx(actor) as conn:
            row = self._one(
                conn,
                "SELECT channel_id, content_hash FROM video_jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            )
            if row is None:
                raise JobNotFound(job_id)
            channel_id, chash = row
            if chash is None:
                raise ApprovalGateError("job has no content to approve")
            try:
                claims = self._verifier.verify(
                    token, job_id=job_id, channel_id=channel_id, content_hash=chash
                )
            except ApprovalError as exc:
                raise ApprovalInvalid(str(exc)) from None
            if self._one(conn, "SELECT 1 FROM approvals WHERE token_jti = %s", (claims.jti,)):
                raise ApprovalReplayError("approval token already used")
            self._set_actor(conn, f"approver:{claims.approver}")
            conn.execute(
                "INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver, "
                "issued_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    job_id,
                    channel_id,
                    chash,
                    claims.jti,
                    claims.approver,
                    claims.issued_at,
                    claims.expires_at,
                ),
            )
            conn.execute("UPDATE video_jobs SET state = 'approved' WHERE id = %s", (job_id,))
        return claims

    def storage_prefix(self, channel_id: str) -> str:
        with self._tx("reader") as conn:
            row = self._one(
                conn, "SELECT storage_prefix FROM channels WHERE id = %s", (channel_id,)
            )
        if row is None:
            raise StoreError(f"unknown channel {channel_id}")
        return str(row[0])

    def record_cost(self, job_id: str, kind: str, cost_cents: int, *, actor: str) -> None:
        with self._tx(actor) as conn:
            cur = conn.execute(
                "INSERT INTO job_costs (job_id, channel_id, kind, cost_cents) "
                "SELECT id, channel_id, %s, %s FROM video_jobs WHERE id = %s",
                (kind, cost_cents, job_id),
            )
            if cur.rowcount != 1:
                raise JobNotFound(job_id)

    # ------------------------------------------------------------------ gateway repository

    def publish_context(self, job_id: str) -> PublishContext:
        with self._tx("gateway") as conn:
            row = self._one(
                conn,
                """
                SELECT j.id::text, j.channel_id, j.state, j.content_hash, j.manifest,
                       c.storage_prefix, c.status, k.status, c.autonomy_tier,
                       c.google_project_id, c.youtube_credentials_ref, g.oauth_client_ref
                FROM video_jobs j
                JOIN channels c ON c.id = j.channel_id
                LEFT JOIN clusters k ON k.id = c.cluster_id
                LEFT JOIN google_projects g ON g.id = c.google_project_id
                WHERE j.id = %s
                """,
                (job_id,),
            )
        if row is None:
            raise JobNotFound(job_id)
        return PublishContext(
            job_id=row[0],
            channel_id=row[1],
            state=JobState(row[2]),
            content_hash=row[3],
            manifest=row[4],
            storage_prefix=row[5],
            channel_status=row[6],
            cluster_status=row[7],
            autonomy_tier=row[8],
            google_project_id=row[9],
            credentials_ref=row[10],
            oauth_client_ref=row[11],
        )

    def approval_consumed(self, job_id: str, jti: str, chash: str) -> bool:
        with self._tx("gateway") as conn:
            row = self._one(
                conn,
                "SELECT 1 FROM approvals WHERE job_id = %s AND token_jti = %s "
                "AND content_hash = %s AND revoked_at IS NULL",
                (job_id, jti, chash),
            )
        return row is not None

    # Constant column list (never user input) interpolated into upload queries.
    _UPLOAD_COLS = "id::text, job_id::text, idempotency_key, status, youtube_video_id"

    @staticmethod
    def _upload(row: Any) -> UploadRecord:
        return UploadRecord(row[0], row[1], row[2], row[3], row[4])

    def find_upload(self, idempotency_key: str) -> UploadRecord | None:
        with self._tx("gateway") as conn:
            row = self._one(
                conn,
                f"SELECT {self._UPLOAD_COLS} FROM uploads WHERE idempotency_key = %s",  # noqa: S608
                (idempotency_key,),
            )
        return None if row is None else self._upload(row)

    def begin_upload(
        self,
        ctx: PublishContext,
        *,
        idempotency_key: str,
        approval_jti: str,
        synthetic_disclosure: bool,
        quota_units: int,
        actor: str,
    ) -> UploadRecord:
        try:
            with self._tx(actor) as conn:
                state = self._one(
                    conn, "SELECT state FROM video_jobs WHERE id = %s FOR UPDATE", (ctx.job_id,)
                )
                if state is None:
                    raise JobNotFound(ctx.job_id)
                if state[0] == JobState.SCHEDULED.value:
                    conn.execute(
                        "UPDATE video_jobs SET state = 'publishing' WHERE id = %s", (ctx.job_id,)
                    )
                conn.execute(
                    "INSERT INTO quota_ledger (google_project_id, units, operation, job_id) "
                    "VALUES (%s, %s, 'videos.insert', %s)",
                    (ctx.google_project_id, quota_units, ctx.job_id),
                )
                row = self._one(
                    conn,
                    "INSERT INTO uploads (job_id, channel_id, idempotency_key, content_hash, "  # noqa: S608 - constant columns
                    "approval_jti, google_project_id, synthetic_disclosure) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    f"RETURNING {self._UPLOAD_COLS}",
                    (
                        ctx.job_id,
                        ctx.channel_id,
                        idempotency_key,
                        ctx.content_hash,
                        approval_jti,
                        ctx.google_project_id,
                        synthetic_disclosure,
                    ),
                )
                return self._upload(row)
        except StoreError as exc:
            cause = exc.__cause__
            if isinstance(cause, pg_errors.UniqueViolation):
                existing = self.find_upload(idempotency_key)
                if existing is not None:
                    # Lost a race with an identical request: report it as already started.
                    return UploadRecord(
                        existing.id,
                        existing.job_id,
                        existing.idempotency_key,
                        existing.status if existing.status != "pending" else "in_progress",
                        existing.youtube_video_id,
                    )
            raise

    def _upload_and_job(
        self, upload_id: str, upload_sql: str, params: tuple[Any, ...], job_state: str, actor: str
    ) -> None:
        with self._tx(actor) as conn:
            row = self._one(conn, upload_sql + " RETURNING job_id", (*params, upload_id))
            if row is None:
                raise UploadGuardError(f"upload {upload_id} not found")
            conn.execute("UPDATE video_jobs SET state = %s WHERE id = %s", (job_state, row[0]))

    def complete_upload(self, upload_id: str, video_id: str, *, actor: str) -> None:
        self._upload_and_job(
            upload_id,
            "UPDATE uploads SET status = 'uploaded', youtube_video_id = %s WHERE id = %s",
            (video_id,),
            JobState.PUBLISHED.value,
            actor,
        )

    def mark_live(self, upload_id: str, *, actor: str) -> None:
        self._upload_and_job(
            upload_id,
            "UPDATE uploads SET status = 'live' WHERE id = %s",
            (),
            JobState.LIVE.value,
            actor,
        )

    def fail_upload(self, upload_id: str, reason: str, *, actor: str) -> None:
        self._upload_and_job(
            upload_id,
            "UPDATE uploads SET status = 'failed', failure_reason = %s WHERE id = %s",
            (reason[:200],),
            JobState.FAILED.value,
            actor,
        )

    def charge_quota(
        self, google_project_id: str, units: int, operation: str, *, job_id: str, actor: str
    ) -> None:
        with self._tx(actor) as conn:
            conn.execute(
                "INSERT INTO quota_ledger (google_project_id, units, operation, job_id) "
                "VALUES (%s, %s, %s, %s)",
                (google_project_id, units, operation, job_id),
            )

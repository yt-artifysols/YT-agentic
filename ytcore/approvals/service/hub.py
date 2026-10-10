"""Approval hub logic: the ONLY place approval tokens are issued.

approve: re-checks the CURRENT content hash, signs a token with the KeyProvider-backed signer,
         consumes it in the DB (approvals row + approved state, atomically) and signals the
         workflow.
edit:    stores a new manifest version with a new content hash, sends the job back to QA and
         signals the workflow. An edit never approves anything.
reject:  records reason + rework stage and signals the workflow.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Protocol

from ytcore.approvals.service.notify import Notification, Notifier
from ytcore.approvals.service.repo import HubConflict, HubRepository, User
from ytcore.approvals.service.signals import WorkflowSignaler
from ytcore.approvals.token import ApprovalClaims, ApprovalSigner
from ytcore.store.errors import ApprovalGateError, ApprovalInvalid, ApprovalReplayError
from ytcore.workflow.models import HubDecision

log = logging.getLogger(__name__)

EDITABLE_FIELDS = {"title", "description", "tags", "script"}
REWORK_STAGES = {"research", "script", "render", "discard"}


class HubError(Exception):
    status = 400


class HubNotFound(HubError):
    status = 404


class HubStale(HubError):
    status = 409


class HubSignalFailed(HubError):
    status = 502


class ApprovalStore(Protocol):
    def verify_approval(self, job_id: str, token: str, *, actor: str) -> ApprovalClaims: ...


@dataclass(frozen=True)
class ApproveResult:
    job_id: str
    content_hash: str
    jti: str
    approver: str


@dataclass(frozen=True)
class EditResult:
    job_id: str
    content_hash: str
    manifest_version: int


@dataclass(frozen=True)
class BatchItemResult:
    job_id: str
    ok: bool
    jti: str | None = None
    error: str | None = None


class ApprovalHub:
    def __init__(
        self,
        *,
        repo: HubRepository,
        store: ApprovalStore,
        signer: ApprovalSigner,
        signaler: WorkflowSignaler,
        notifier: Notifier,
        token_ttl: timedelta,
        batch_max_items: int = 25,
        link: str = "",
    ) -> None:
        self._repo = repo
        self._store = store
        self._signer = signer
        self._signaler = signaler
        self._notifier = notifier
        self._ttl = token_ttl
        self._batch_max = batch_max_items
        self._link = link

    async def _signal(self, job_id: str, decision: HubDecision) -> None:
        try:
            await self._signaler.send(job_id, decision)
        except Exception as exc:
            log.error("workflow signal failed for job %s: %s", job_id, type(exc).__name__)
            raise HubSignalFailed(
                "decision recorded but the workflow could not be signalled; retry later"
            ) from exc

    async def approve(self, job_id: str, expected_hash: str, user: User) -> ApproveResult:
        head = await asyncio.to_thread(self._repo.head, job_id)
        if head is None:
            raise HubNotFound("job not found")
        if head.state != "awaiting_approval" or head.content_hash != expected_hash:
            raise HubStale("content changed since the preview was loaded; reload and review again")
        token, claims = self._signer.issue(
            job_id=job_id,
            channel_id=head.channel_id,
            content_hash=expected_hash,
            approver=user.username,
            ttl=self._ttl,
        )
        try:
            # Consumes the jti and approves in one transaction; fails if the content changed.
            await asyncio.to_thread(
                self._store.verify_approval, job_id, token, actor=f"hub:{user.username}"
            )
        except (ApprovalInvalid, ApprovalGateError, ApprovalReplayError) as exc:
            raise HubStale(f"approval refused: {exc}") from exc
        await asyncio.to_thread(
            self._repo.audit,
            f"hub:{user.username}",
            "hub.approve",
            f"job:{job_id}",
            {"jti": claims.jti, "content_hash": expected_hash},
        )
        await self._signal(
            job_id, HubDecision("approve", user.username, expected_hash, token=token)
        )
        return ApproveResult(job_id, expected_hash, claims.jti, user.username)

    async def edit(
        self, job_id: str, expected_hash: str, changes: dict[str, Any], user: User
    ) -> EditResult:
        unknown = set(changes) - EDITABLE_FIELDS
        if unknown or not changes:
            raise HubError(f"editable fields are {sorted(EDITABLE_FIELDS)}")
        try:
            new_hash, version = await asyncio.to_thread(
                self._repo.apply_edit,
                job_id,
                expected_hash,
                changes,
                actor=f"hub:{user.username}",
            )
        except HubConflict as exc:
            raise HubStale(str(exc)) from exc
        await self._signal(job_id, HubDecision("edit", user.username, new_hash))
        return EditResult(job_id, new_hash, version)

    async def reject(
        self, job_id: str, expected_hash: str, reason: str, rework_stage: str, user: User
    ) -> None:
        if rework_stage not in REWORK_STAGES:
            raise HubError(f"rework_stage must be one of {sorted(REWORK_STAGES)}")
        if len(reason.strip()) < 10:
            raise HubError("a rejection needs a reason of at least 10 characters")
        try:
            await asyncio.to_thread(
                self._repo.record_rejection,
                job_id,
                expected_hash,
                reason.strip(),
                rework_stage,
                username=user.username,
            )
        except HubConflict as exc:
            raise HubStale(str(exc)) from exc
        await self._signal(
            job_id,
            HubDecision(
                "reject", user.username, expected_hash, rework_stage=rework_stage, reason=reason
            ),
        )

    async def batch_approve(
        self, items: list[tuple[str, str]], user: User
    ) -> list[BatchItemResult]:
        """Fast lane only. Each video gets its own token and its own hash check."""
        if len(items) > self._batch_max:
            raise HubError(f"at most {self._batch_max} videos per batch")
        results: list[BatchItemResult] = []
        for job_id, expected_hash in items:
            if not await asyncio.to_thread(self._repo.is_fast_lane, job_id):
                results.append(BatchItemResult(job_id, False, error="not eligible for fast lane"))
                continue
            try:
                res = await self.approve(job_id, expected_hash, user)
            except HubError as exc:
                results.append(BatchItemResult(job_id, False, error=str(exc)))
            else:
                results.append(BatchItemResult(job_id, True, jti=res.jti))
        await asyncio.to_thread(
            self._repo.audit,
            f"hub:{user.username}",
            "hub.batch_approve",
            "batch",
            {
                "ok": [r.job_id for r in results if r.ok],
                "failed": [r.job_id for r in results if not r.ok],
            },
        )
        return results

    async def set_kill_switch(
        self, scope: str, ident: str | None, paused: bool, reason: str, user: User
    ) -> None:
        actor = f"hub:{user.username}"
        if scope == "global":
            await asyncio.to_thread(self._repo.set_global_pause, paused, actor=actor)
        elif scope in {"cluster", "channel"}:
            assert ident is not None
            table: Literal["clusters", "channels"] = (
                "clusters" if scope == "cluster" else "channels"
            )
            found = await asyncio.to_thread(
                self._repo.set_status, table, ident, paused, actor=actor
            )
            if not found:
                raise HubNotFound(f"{scope} not found")
        else:
            raise HubError("scope must be global, cluster or channel")
        await asyncio.to_thread(
            self._repo.audit,
            actor,
            "hub.killswitch",
            f"{scope}:{ident or '*'}",
            {"paused": paused, "reason": reason},
        )
        verb = "ENGAGED" if paused else "released"
        await asyncio.to_thread(
            self._notifier.send,
            Notification(
                "kill_switch",
                f"Kill switch {verb}: {scope}{' ' + ident if ident else ''}",
                f"By {user.username}. Reason: {reason or '-'}",
                f"{self._link}/controls" if self._link else "",
            ),
        )

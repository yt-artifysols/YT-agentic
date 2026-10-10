"""Knowledge repositories bound to ONE channel.

Every query filters on the bound channel id; there is no method that takes another channel, so
cross-channel reads are impossible by construction (and the database enforces the same with
composite foreign keys). There is deliberately no approve method here: approval lives only in
the approval hub (ytcore/approvals/service/repo.py).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

import psycopg

from ytcore.knowledge.models import KnowledgeDoc, KnowledgeKind, KnowledgeSnapshot
from ytcore.safety.scrub import SecretInInput, ensure_no_secrets
from ytcore.store.errors import KnowledgeForbidden, KnowledgeStale, StoreError
from ytcore.store.postgres import Connect, map_db_error


class ChannelKnowledge(Protocol):
    channel_id: str

    def latest_approved(self) -> KnowledgeSnapshot: ...

    def propose(
        self, kind: KnowledgeKind, content: str, *, proposed_by: str, base_version: int | None
    ) -> KnowledgeDoc: ...

    def pin(self, job_id: str, snapshot: KnowledgeSnapshot, *, actor: str) -> None: ...

    def pinned(self, job_id: str) -> KnowledgeSnapshot: ...

    def drafts(self) -> list[KnowledgeDoc]: ...


def _no_secrets(content: str) -> None:
    try:
        ensure_no_secrets(content, "knowledge content")
    except SecretInInput as exc:
        raise StoreError(str(exc)) from None


_COLS = (
    "id::text, channel_id, kind, version, content, content_sha256, status, proposed_by, "
    "base_version, proposed_at, approved_by, approved_at, rejected_by, reject_reason"
)


def doc_from_row(row: Any) -> KnowledgeDoc:
    return KnowledgeDoc(
        id=row[0],
        channel_id=row[1],
        kind=KnowledgeKind(row[2]),
        version=row[3],
        content=row[4],
        content_sha256=row[5],
        status=row[6],
        proposed_by=row[7],
        base_version=row[8],
        proposed_at=row[9],
        approved_by=row[10],
        approved_at=row[11],
        rejected_by=row[12],
        reject_reason=row[13],
    )


class PostgresChannelKnowledge:
    def __init__(self, connect: Connect, channel_id: str) -> None:
        self._connect = connect
        self.channel_id = channel_id

    @contextmanager
    def _tx(self, actor: str) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (actor,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    def latest_approved(self) -> KnowledgeSnapshot:
        with self._tx("reader") as conn:
            rows = conn.execute(
                f"SELECT DISTINCT ON (kind) {_COLS} FROM knowledge_docs "  # noqa: S608 - constant
                "WHERE channel_id = %s AND status = 'approved' ORDER BY kind, version DESC",
                (self.channel_id,),
            ).fetchall()
        return KnowledgeSnapshot(self.channel_id, {d.kind: d for d in map(doc_from_row, rows)})

    def propose(
        self, kind: KnowledgeKind, content: str, *, proposed_by: str, base_version: int | None
    ) -> KnowledgeDoc:
        _no_secrets(content)
        with self._tx(proposed_by) as conn:
            row = conn.execute(
                "INSERT INTO knowledge_docs (channel_id, kind, version, base_version, content, "  # noqa: S608
                "content_sha256, proposed_by) VALUES (%s, %s, 0, %s, %s, %s, %s) "
                f"RETURNING {_COLS}",
                (
                    self.channel_id,
                    kind.value,
                    base_version,
                    content,
                    "sha256:" + "0" * 64,  # replaced by the database
                    proposed_by,
                ),
            ).fetchone()
        return doc_from_row(row)

    def pin(self, job_id: str, snapshot: KnowledgeSnapshot, *, actor: str) -> None:
        if snapshot.channel_id != self.channel_id:
            raise StoreError("snapshot belongs to another channel")
        with self._tx(actor) as conn:
            for doc_id in snapshot.doc_ids():
                conn.execute(
                    "INSERT INTO job_knowledge (job_id, channel_id, doc_id, kind, version) "
                    "VALUES (%s, %s, %s, '', 0) ON CONFLICT DO NOTHING",
                    (job_id, self.channel_id, doc_id),
                )

    def pinned(self, job_id: str) -> KnowledgeSnapshot:
        with self._tx("reader") as conn:
            rows = conn.execute(
                f"SELECT DISTINCT ON (d.kind) {', '.join('d.' + c for c in _COLS.split(', '))} "  # noqa: S608
                "FROM job_knowledge p JOIN knowledge_docs d ON d.id = p.doc_id "
                "WHERE p.job_id = %s AND p.channel_id = %s AND d.channel_id = %s "
                "ORDER BY d.kind, p.pinned_at DESC, d.version DESC",
                (job_id, self.channel_id, self.channel_id),
            ).fetchall()
        return KnowledgeSnapshot(self.channel_id, {d.kind: d for d in map(doc_from_row, rows)})

    def drafts(self) -> list[KnowledgeDoc]:
        with self._tx("reader") as conn:
            rows = conn.execute(
                f"SELECT {_COLS} FROM knowledge_docs WHERE channel_id = %s "  # noqa: S608
                "AND status = 'draft' ORDER BY proposed_at",
                (self.channel_id,),
            ).fetchall()
        return [doc_from_row(r) for r in rows]


class PostgresKnowledge:
    """Hands out channel-bound knowledge repositories."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    @classmethod
    def from_conninfo(cls, conninfo: str) -> PostgresKnowledge:
        return cls(lambda: psycopg.connect(conninfo, autocommit=True))

    def channel(self, channel_id: str) -> PostgresChannelKnowledge:
        return PostgresChannelKnowledge(self._connect, channel_id)


# ----------------------------------------------------------------------------- in memory


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


@dataclass
class InMemoryKnowledge:
    """Dev/test/eval knowledge base mirroring the database rules."""

    human_approvers: set[str] = field(default_factory=set)
    docs: dict[str, KnowledgeDoc] = field(default_factory=dict)
    pins: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # job -> (chan, doc)

    def channel(self, channel_id: str) -> InMemoryChannelKnowledge:
        return InMemoryChannelKnowledge(self, channel_id)

    def _latest(self, channel_id: str, kind: KnowledgeKind) -> int | None:
        versions = [
            d.version
            for d in self.docs.values()
            if d.channel_id == channel_id and d.kind == kind and d.status == "approved"
        ]
        return max(versions, default=None)

    def decide(self, doc_id: str, *, approver: str, approve: bool, reason: str = "") -> None:
        """Human decision (what the hub does). Refuses anyone who is not a human approver."""
        doc = self.docs[doc_id]
        if approver not in self.human_approvers:
            raise KnowledgeForbidden("knowledge approval requires an active human approver")
        if doc.status != "draft":
            raise KnowledgeForbidden(f"knowledge version {doc.version} is {doc.status}")
        now = datetime.now(UTC)
        if approve:
            if doc.base_version != self._latest(doc.channel_id, doc.kind):
                raise KnowledgeStale("knowledge draft is stale")
            self.docs[doc_id] = replace(
                doc, status="approved", approved_by=approver, approved_at=now
            )
        else:
            self.docs[doc_id] = replace(
                doc, status="rejected", rejected_by=approver, reject_reason=reason
            )

    def seed_approved(
        self, channel_id: str, kind: KnowledgeKind, content: str, *, approver: str = "owner"
    ) -> KnowledgeDoc:
        """Dev/test/eval helper: a human-approved version, proposed and approved by `approver`."""
        self.human_approvers.add(approver)
        chan = self.channel(channel_id)
        doc = chan.propose(
            kind, content, proposed_by=approver, base_version=self._latest(channel_id, kind)
        )
        self.decide(doc.id, approver=approver, approve=True)
        return self.docs[doc.id]


@dataclass
class InMemoryChannelKnowledge:
    base: InMemoryKnowledge
    channel_id: str

    def latest_approved(self) -> KnowledgeSnapshot:
        best: dict[KnowledgeKind, KnowledgeDoc] = {}
        for d in self.base.docs.values():
            if d.channel_id != self.channel_id or d.status != "approved":
                continue
            if d.kind not in best or d.version > best[d.kind].version:
                best[d.kind] = d
        return KnowledgeSnapshot(self.channel_id, best)

    def propose(
        self, kind: KnowledgeKind, content: str, *, proposed_by: str, base_version: int | None
    ) -> KnowledgeDoc:
        if not content or len(content) > 100_000:
            raise StoreError("knowledge content must be 1..100000 characters")
        _no_secrets(content)
        mine = [d for d in self.base.docs.values() if d.channel_id == self.channel_id]
        if base_version is not None and not any(
            d.kind == kind and d.version == base_version and d.status == "approved" for d in mine
        ):
            raise StoreError(f"knowledge base_version {base_version} is not an approved version")
        version = 1 + max((d.version for d in mine if d.kind == kind), default=0)
        doc = KnowledgeDoc(
            id=str(uuid.uuid4()),
            channel_id=self.channel_id,
            kind=kind,
            version=version,
            content=content,
            content_sha256=_sha(content),
            status="draft",
            proposed_by=proposed_by,
            base_version=base_version,
            proposed_at=datetime.now(UTC),
        )
        self.base.docs[doc.id] = doc
        return doc

    def pin(self, job_id: str, snapshot: KnowledgeSnapshot, *, actor: str) -> None:
        if snapshot.channel_id != self.channel_id:
            raise StoreError("snapshot belongs to another channel")
        pins = self.base.pins.setdefault(job_id, [])
        for doc_id in snapshot.doc_ids():
            doc = self.base.docs[doc_id]
            if doc.status != "approved" or doc.channel_id != self.channel_id:
                raise KnowledgeForbidden("jobs may only use approved knowledge")
            pins.append((self.channel_id, doc_id))

    def pinned(self, job_id: str) -> KnowledgeSnapshot:
        best: dict[KnowledgeKind, KnowledgeDoc] = {}
        for chan, doc_id in self.base.pins.get(job_id, []):  # later pins win
            if chan == self.channel_id:
                doc = self.base.docs[doc_id]
                best[doc.kind] = doc
        return KnowledgeSnapshot(self.channel_id, best)

    def drafts(self) -> list[KnowledgeDoc]:
        return [
            d
            for d in self.base.docs.values()
            if d.channel_id == self.channel_id and d.status == "draft"
        ]

"""Owner input repositories (Postgres and in-memory)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

import psycopg
from psycopg import errors as pg_errors

from ytcore.inputs.models import CONSUMABLE, InputKind, OwnerInput, validate_input
from ytcore.store.errors import InputRejected, StoreError
from ytcore.store.postgres import Connect, map_db_error

# One idea per video, plus up to this many notes / footage items.
MAX_SUPPORTING = 2


class InputStore(Protocol):
    def add(
        self,
        channel_id: str,
        kind: InputKind,
        *,
        body: str | None = None,
        uri: str | None = None,
        sha256: str | None = None,
        submitted_by: str,
    ) -> OwnerInput: ...

    def list_for(self, channel_id: str, status: str | None = None) -> list[OwnerInput]: ...

    def goals(self, channel_id: str) -> list[OwnerInput]: ...

    def claim_for_job(self, job_id: str, channel_id: str, *, actor: str) -> list[OwnerInput]:
        """Inputs for this job: the ones already linked (rework), else the oldest new ones.

        Links (and so marks used) the consumed inputs and links all active goals as context.
        """
        ...

    def archive(self, channel_id: str, input_id: str, *, actor: str) -> None: ...


_COLS = (
    "id::text, channel_id, kind, uri, body, sha256, status, submitted_by, used_by_job::text, "
    "created_at"
)


def _row(row: Any) -> OwnerInput:
    return OwnerInput(
        id=row[0],
        channel_id=row[1],
        kind=InputKind(row[2]),
        uri=row[3],
        body=row[4],
        sha256=row[5],
        status=row[6],
        submitted_by=row[7],
        used_by_job=row[8],
        created_at=row[9],
    )


def _pick(new: list[OwnerInput]) -> list[OwnerInput]:
    """Oldest idea first, then the oldest notes/footage; without an idea, notes/footage only."""
    ideas = [i for i in new if i.kind is InputKind.IDEA][:1]
    rest = [i for i in new if i.kind in (InputKind.NOTE, InputKind.FOOTAGE)]
    return ideas + rest[: MAX_SUPPORTING if ideas else MAX_SUPPORTING + 1]


class PostgresInputs:
    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    @classmethod
    def from_conninfo(cls, conninfo: str) -> PostgresInputs:
        return cls(lambda: psycopg.connect(conninfo, autocommit=True))

    @contextmanager
    def _tx(self, actor: str) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (actor,))
                yield conn
        except pg_errors.UniqueViolation as exc:
            raise InputRejected("this input is already queued for the channel") from exc
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    def add(
        self,
        channel_id: str,
        kind: InputKind,
        *,
        body: str | None = None,
        uri: str | None = None,
        sha256: str | None = None,
        submitted_by: str,
    ) -> OwnerInput:
        try:
            kind = InputKind(kind)
            digest = validate_input(kind, body, uri, sha256)
        except ValueError as exc:
            raise InputRejected(str(exc)) from None
        with self._tx(submitted_by) as conn:
            row = conn.execute(
                "INSERT INTO owner_inputs (channel_id, kind, uri, body, sha256, submitted_by) "  # noqa: S608
                f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
                (channel_id, kind.value, uri, body, digest, submitted_by),
            ).fetchone()
        return _row(row)

    def list_for(self, channel_id: str, status: str | None = None) -> list[OwnerInput]:
        with self._tx("reader") as conn:
            rows = conn.execute(
                f"SELECT {_COLS} FROM owner_inputs WHERE channel_id = %s "  # noqa: S608
                "AND (%s::text IS NULL OR status = %s) ORDER BY created_at, id",
                (channel_id, status, status),
            ).fetchall()
        return [_row(r) for r in rows]

    def goals(self, channel_id: str) -> list[OwnerInput]:
        return [i for i in self.list_for(channel_id, "new") if i.kind is InputKind.GOAL]

    def claim_for_job(self, job_id: str, channel_id: str, *, actor: str) -> list[OwnerInput]:
        with self._tx(actor) as conn:
            linked = conn.execute(
                f"SELECT {', '.join('i.' + c for c in _COLS.split(', '))} "  # noqa: S608
                "FROM job_owner_inputs l JOIN owner_inputs i ON i.id = l.input_id "
                "WHERE l.job_id = %s AND l.channel_id = %s AND i.kind <> 'goal' "
                "ORDER BY i.created_at, i.id",
                (job_id, channel_id),
            ).fetchall()
            if linked:
                picked = [_row(r) for r in linked]
            else:
                new = conn.execute(
                    f"SELECT {_COLS} FROM owner_inputs WHERE channel_id = %s "  # noqa: S608
                    "AND status = 'new' AND kind <> 'goal' ORDER BY created_at, id "
                    "LIMIT 50 FOR UPDATE SKIP LOCKED",
                    (channel_id,),
                ).fetchall()
                picked = _pick([_row(r) for r in new])
            goals = conn.execute(
                "SELECT id::text FROM owner_inputs WHERE channel_id = %s AND kind = 'goal' "
                "AND status = 'new'",
                (channel_id,),
            ).fetchall()
            for input_id in [i.id for i in picked] + [g[0] for g in goals]:
                conn.execute(
                    "INSERT INTO job_owner_inputs (job_id, channel_id, input_id) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (job_id, channel_id, input_id),
                )
        return [replace(i, status="used", used_by_job=job_id) for i in picked]

    def archive(self, channel_id: str, input_id: str, *, actor: str) -> None:
        with self._tx(actor) as conn:
            cur = conn.execute(
                "UPDATE owner_inputs SET status = 'archived' WHERE id = %s AND channel_id = %s",
                (input_id, channel_id),
            )
            if cur.rowcount != 1:
                raise StoreError("owner input not found for this channel")


@dataclass
class InMemoryInputs:
    inputs: dict[str, OwnerInput] = field(default_factory=dict)
    links: dict[str, list[str]] = field(default_factory=dict)  # job -> input ids

    def add(
        self,
        channel_id: str,
        kind: InputKind,
        *,
        body: str | None = None,
        uri: str | None = None,
        sha256: str | None = None,
        submitted_by: str,
    ) -> OwnerInput:
        try:
            kind = InputKind(kind)
            digest = validate_input(kind, body, uri, sha256)
        except ValueError as exc:
            raise InputRejected(str(exc)) from None
        if any(
            i.channel_id == channel_id and i.kind == kind and i.sha256 == digest
            for i in self.inputs.values()
            if i.status != "archived"
        ):
            raise InputRejected("this input is already queued for the channel")
        item = OwnerInput(
            id=str(uuid.uuid4()),
            channel_id=channel_id,
            kind=kind,
            uri=uri,
            body=body,
            sha256=digest,
            status="new",
            submitted_by=submitted_by,
            created_at=datetime.now(UTC),
        )
        self.inputs[item.id] = item
        return item

    def list_for(self, channel_id: str, status: str | None = None) -> list[OwnerInput]:
        return [
            i
            for i in self.inputs.values()
            if i.channel_id == channel_id and (status is None or i.status == status)
        ]

    def goals(self, channel_id: str) -> list[OwnerInput]:
        return [i for i in self.list_for(channel_id, "new") if i.kind is InputKind.GOAL]

    def claim_for_job(self, job_id: str, channel_id: str, *, actor: str) -> list[OwnerInput]:
        linked = [
            self.inputs[i]
            for i in self.links.get(job_id, [])
            if self.inputs[i].kind in CONSUMABLE and self.inputs[i].channel_id == channel_id
        ]
        new = [i for i in self.list_for(channel_id, "new") if i.kind in CONSUMABLE]
        picked = linked or _pick(new)
        ids = self.links.setdefault(job_id, [])
        for item in picked + self.goals(channel_id):
            if item.id not in ids:
                ids.append(item.id)
            if item.kind in CONSUMABLE:
                self.inputs[item.id] = replace(item, status="used", used_by_job=job_id)
        return [self.inputs[i.id] for i in picked]

    def archive(self, channel_id: str, input_id: str, *, actor: str) -> None:
        item = self.inputs.get(input_id)
        if item is None or item.channel_id != channel_id:
            raise StoreError("owner input not found for this channel")
        self.inputs[input_id] = replace(item, status="archived")

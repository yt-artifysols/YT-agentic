"""Per-channel vector memory.

A ChannelMemory is bound to one channel and that channel's own namespace (looked up from the
channels table, never supplied by a caller). Every query filters on both, and the database's
composite foreign key refuses rows whose namespace belongs to another channel.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg

from ytcore.store.errors import StoreError
from ytcore.store.postgres import Connect, map_db_error

MEMORY_KINDS = frozenset({"research_note", "fact", "script", "hook", "performance"})


class Embedder(Protocol):
    model: str

    def embed(self, text: str) -> list[float]: ...


class HashEmbedder:
    """Deterministic bag-of-words embedder for dev, tests and evals."""

    model = "hash-bow-64"
    dims = 64

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            h = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big")
            vec[h % self.dims] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


@dataclass(frozen=True)
class MemoryItem:
    kind: str
    content: str
    score: float


class ChannelMemory(Protocol):
    channel_id: str
    namespace: str

    def add(self, kind: str, content: str, *, source_job: str | None = None) -> None: ...

    def search(self, query: str, *, k: int = 5) -> list[MemoryItem]: ...


def _vector(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


class PostgresChannelMemory:
    def __init__(self, connect: Connect, channel_id: str, namespace: str, embedder: Embedder):
        self._connect, self._embedder = connect, embedder
        self.channel_id, self.namespace = channel_id, namespace

    @contextmanager
    def _tx(self, actor: str) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (actor,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    def add(self, kind: str, content: str, *, source_job: str | None = None) -> None:
        if kind not in MEMORY_KINDS:
            raise StoreError(f"unknown memory kind {kind!r}")
        with self._tx("memory") as conn:
            conn.execute(
                "INSERT INTO channel_memory (channel_id, namespace, kind, content, "
                "embedding_model, embedding, source_job) "
                "VALUES (%s, %s, %s, %s, %s, %s::vector, %s)",
                (
                    self.channel_id,
                    self.namespace,
                    kind,
                    content[:20_000],
                    self._embedder.model,
                    _vector(self._embedder.embed(content)),
                    source_job,
                ),
            )

    def search(self, query: str, *, k: int = 5) -> list[MemoryItem]:
        with self._tx("memory") as conn:
            rows = conn.execute(
                "SELECT kind, content, 1 - (embedding <=> %s::vector) FROM channel_memory "
                "WHERE channel_id = %s AND namespace = %s AND embedding_model = %s "
                "ORDER BY embedding <=> %s::vector, id LIMIT %s",
                (
                    (vec := _vector(self._embedder.embed(query))),
                    self.channel_id,
                    self.namespace,
                    self._embedder.model,
                    vec,
                    k,
                ),
            ).fetchall()
        return [MemoryItem(r[0], r[1], float(r[2] or 0.0)) for r in rows]


class PostgresMemory:
    """Hands out memories bound to a channel and the namespace the database assigns to it."""

    def __init__(self, connect: Connect, embedder: Embedder | None = None) -> None:
        self._connect = connect
        self._embedder = embedder or HashEmbedder()

    def for_channel(self, channel_id: str) -> PostgresChannelMemory:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT memory_namespace FROM channels WHERE id = %s", (channel_id,)
                ).fetchone()
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc
        if row is None:
            raise StoreError(f"unknown channel {channel_id}")
        return PostgresChannelMemory(self._connect, channel_id, row[0], self._embedder)


@dataclass
class InMemoryMemory:
    namespaces: dict[str, str] = field(default_factory=dict)  # channel -> namespace
    rows: list[tuple[str, str, str, str, list[float]]] = field(default_factory=list)
    embedder: Embedder = field(default_factory=HashEmbedder)

    def for_channel(self, channel_id: str) -> InMemoryChannelMemory:
        namespace = self.namespaces.setdefault(channel_id, f"mem:{channel_id}")
        return InMemoryChannelMemory(self, channel_id, namespace)


@dataclass
class InMemoryChannelMemory:
    base: InMemoryMemory
    channel_id: str
    namespace: str

    def add(self, kind: str, content: str, *, source_job: str | None = None) -> None:
        if kind not in MEMORY_KINDS:
            raise StoreError(f"unknown memory kind {kind!r}")
        if self.base.namespaces.get(self.channel_id) != self.namespace:
            raise StoreError("namespace does not belong to this channel")
        self.base.rows.append(
            (self.channel_id, self.namespace, kind, content, self.base.embedder.embed(content))
        )

    def search(self, query: str, *, k: int = 5) -> list[MemoryItem]:
        q = self.base.embedder.embed(query)
        scored = [
            MemoryItem(kind, content, sum(a * b for a, b in zip(q, vec, strict=True)))
            for chan, ns, kind, content, vec in self.base.rows
            if chan == self.channel_id and ns == self.namespace
        ]
        return sorted(scored, key=lambda m: -m.score)[:k]

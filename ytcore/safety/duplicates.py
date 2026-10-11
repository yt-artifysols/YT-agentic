"""Cross-channel duplicate detection.

Signals: script near-duplicates (word-shingle MinHash + embedding cosine), reused generated media
(sha256 of voiceover, video and thumbnail), repeated title templates and repeated thumbnail text.
The index spans ALL channels by design (that is the point), so it is a platform service: only QA
CODE calls it, matches go to human approvers as flags, and no match detail ever enters a model
prompt (the block reason that reaches the script writer is generic).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import psycopg

from ytcore.agents.memory import Embedder
from ytcore.safety.policy import DuplicateThresholds
from ytcore.safety.textutil import STOP
from ytcore.safety.textutil import words as _words
from ytcore.store.postgres import Connect, map_db_error

SHINGLE_WORDS = 5
MINHASH_PERMS = 64
_P = (1 << 61) - 1  # Mersenne prime for the universal hash family

Kind = Literal["script", "media", "title_template", "thumbnail_text"]


words = _words


def shingles(text: str, k: int = SHINGLE_WORDS) -> set[int]:
    toks = words(text)
    if len(toks) < k:
        toks = toks + [""] * (k - len(toks))
    return {
        int.from_bytes(
            hashlib.blake2b(" ".join(toks[i : i + k]).encode(), digest_size=8).digest(), "big"
        )
        for i in range(len(toks) - k + 1)
    }


def _perm(i: int) -> tuple[int, int]:
    seed = hashlib.blake2b(f"minhash-{i}".encode(), digest_size=16).digest()
    return int.from_bytes(seed[:8], "big") | 1, int.from_bytes(seed[8:], "big")


_PERMS = [_perm(i) for i in range(MINHASH_PERMS)]


def minhash(shingle_set: set[int]) -> list[int]:
    if not shingle_set:
        return [0] * MINHASH_PERMS
    return [min((a * h + b) % _P for h in shingle_set) for a, b in _PERMS]


def jaccard_estimate(a: list[int], b: list[int]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a)


def jaccard(a: set[int], b: set[int]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def text_similarity(a: str, b: str) -> float:
    """Exact word-shingle Jaccard of two texts (used against the channel's own history)."""
    return jaccard(shingles(a), shingles(b))


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) if len(a) == len(b) else 0.0


def title_template(title: str) -> str:
    """Title with its topic words masked: "Why index funds beat stock picking" -> "why _"."""
    out: list[str] = []
    for tok in words(title):
        part = tok if tok in STOP else "_"
        if part == "_" and out and out[-1] == "_":
            continue
        out.append(part)
    return " ".join(out)


def normalized_text_key(text: str) -> str:
    return hashlib.sha256(" ".join(words(text)).encode()).hexdigest()


@dataclass(frozen=True)
class Fingerprints:
    script_text: str
    embedding: list[float]
    embedding_model: str
    minhash: list[int]
    media: dict[str, str]  # asset name -> sha256, GENERATED assets only (not licensed stock)
    title_template: str
    thumbnail_text_key: str

    @classmethod
    def build(
        cls,
        *,
        script_text: str,
        title: str,
        thumbnail_text: str,
        media: dict[str, str],
        embedder: Embedder,
    ) -> Fingerprints:
        return cls(
            script_text,
            embedder.embed(script_text),
            embedder.model,
            minhash(shingles(script_text)),
            dict(media),
            title_template(title),
            normalized_text_key(thumbnail_text) if thumbnail_text.strip() else "",
        )


@dataclass(frozen=True)
class DuplicateMatch:
    kind: Kind
    action: Literal["escalate", "block"]
    score: float  # similarity (0..1) for scripts, 1.0 for media, the count for templates
    other_job: str
    other_channel: str
    shingle: float = 0.0
    embedding: float = 0.0
    count: int = 0
    label: str = ""

    def evidence(self) -> str:
        """For human approvers only. Never put this in a prompt."""
        if self.kind == "script":
            return (
                f"script resembles job {self.other_job} on channel {self.other_channel} "
                f"(shingles {self.shingle:.2f}, embedding {self.embedding:.2f})"
            )
        if self.kind == "media":
            return (
                f"{self.label} is identical to media of job {self.other_job} "
                f"on channel {self.other_channel}"
            )
        return (
            f"{self.kind} repeated in {self.count} jobs of other channels "
            f"(e.g. {self.other_channel})"
        )


def classify_script(
    shingle: float, emb: float, t: DuplicateThresholds
) -> Literal["escalate", "block"] | None:
    if shingle >= t.shingle_block or emb >= t.embedding_block:
        return "block"
    if shingle >= t.shingle_escalate or emb >= t.embedding_escalate:
        return "escalate"
    return None


def classify_count(count: int, escalate: int, block: int) -> Literal["escalate", "block"] | None:
    if count >= block:
        return "block"
    if count >= escalate:
        return "escalate"
    return None


def _count_specs(fp: Fingerprints, t: DuplicateThresholds) -> list[tuple[Kind, str, int, int]]:
    return [
        ("title_template", fp.title_template, t.title_template_escalate, t.title_template_block),
        (
            "thumbnail_text",
            fp.thumbnail_text_key,
            t.thumbnail_text_escalate,
            t.thumbnail_text_block,
        ),
    ]


@dataclass(frozen=True)
class DuplicateReport:
    matches: list[DuplicateMatch]
    # Highest word-shingle similarity to another channel's script, even below any threshold.
    script_similarity: float = 0.0


class FingerprintIndex(Protocol):
    def find(
        self, *, job_id: str, channel_id: str, fp: Fingerprints, thresholds: DuplicateThresholds
    ) -> DuplicateReport:
        """Matches against OTHER channels' content (never the caller's own channel)."""
        ...

    def record(self, *, job_id: str, channel_id: str, fp: Fingerprints) -> None: ...


@dataclass
class _Row:
    job_id: str
    channel_id: str
    kind: str
    key: str
    label: str = ""
    minhash: list[int] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)
    model: str = ""


@dataclass
class InMemoryFingerprintIndex:
    rows: list[_Row] = field(default_factory=list)

    def record(self, *, job_id: str, channel_id: str, fp: Fingerprints) -> None:
        def add(kind: str, key: str, **kw: Any) -> None:
            if key and not any(
                r.job_id == job_id and r.kind == kind and r.key == key for r in self.rows
            ):
                self.rows.append(_Row(job_id, channel_id, kind, key, **kw))

        add("script", hashlib.sha256(fp.script_text.encode()).hexdigest(), minhash=fp.minhash,
            embedding=fp.embedding, model=fp.embedding_model)  # fmt: skip
        for name, digest in fp.media.items():
            add("media", digest, label=name)
        add("title_template", fp.title_template)
        add("thumbnail_text", fp.thumbnail_text_key)

    def find(
        self, *, job_id: str, channel_id: str, fp: Fingerprints, thresholds: DuplicateThresholds
    ) -> DuplicateReport:
        others = [r for r in self.rows if r.channel_id != channel_id]
        out: list[DuplicateMatch] = []
        top = 0.0
        best: DuplicateMatch | None = None
        for r in (r for r in others if r.kind == "script" and r.model == fp.embedding_model):
            sh, em = jaccard_estimate(fp.minhash, r.minhash), cosine(fp.embedding, r.embedding)
            top = max(top, sh)
            action = classify_script(sh, em, thresholds)
            if action and (best is None or max(sh, em) > best.score):
                best = DuplicateMatch("script", action, max(sh, em), r.job_id, r.channel_id, sh, em)
        if best:
            out.append(best)
        for name, digest in fp.media.items():
            hit = next((r for r in others if r.kind == "media" and r.key == digest), None)
            if hit:
                out.append(
                    DuplicateMatch("media", "block", 1.0, hit.job_id, hit.channel_id, label=name)
                )
        for kind, key, esc, blk in _count_specs(fp, thresholds):
            hits = {r.job_id: r for r in others if r.kind == kind and r.key == key} if key else {}
            action = classify_count(len(hits), esc, blk)
            if action:
                first = next(iter(hits.values()))
                out.append(
                    DuplicateMatch(
                        kind,
                        action,
                        float(len(hits)),
                        first.job_id,
                        first.channel_id,
                        count=len(hits),
                    )
                )
        return DuplicateReport(out, top)


def _vec(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


class PostgresFingerprintIndex:
    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    @contextmanager
    def _tx(self, actor: str) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (actor,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    def record(self, *, job_id: str, channel_id: str, fp: Fingerprints) -> None:
        with self._tx("safety") as conn:

            def add(kind: str, key: str, label: str = "", *, sig: list[int] | None = None) -> None:
                if not key:
                    return
                conn.execute(
                    "INSERT INTO content_fingerprints (job_id, channel_id, kind, key, label, "
                    "minhash, embedding, embedding_model) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s) "
                    "ON CONFLICT (job_id, kind, key) DO NOTHING",
                    (job_id, channel_id, kind, key, label, sig,
                     _vec(fp.embedding) if sig is not None else None,
                     fp.embedding_model if sig is not None else None),
                )  # fmt: skip

            add("script", hashlib.sha256(fp.script_text.encode()).hexdigest(), sig=fp.minhash)
            for name, digest in fp.media.items():
                add("media", digest, name)
            add("title_template", fp.title_template)
            add("thumbnail_text", fp.thumbnail_text_key)

    def find(
        self, *, job_id: str, channel_id: str, fp: Fingerprints, thresholds: DuplicateThresholds
    ) -> DuplicateReport:
        out: list[DuplicateMatch] = []
        top = 0.0
        window = f"{int(thresholds.window_days)} days"
        with self._tx("safety") as conn:
            rows = conn.execute(
                "SELECT job_id::text, channel_id, minhash, 1 - (embedding <=> %s::vector) "
                "FROM content_fingerprints WHERE kind = 'script' AND channel_id <> %s "
                "AND embedding_model = %s AND vector_dims(embedding) = %s "
                "ORDER BY embedding <=> %s::vector LIMIT 25",
                (
                    _vec(fp.embedding),
                    channel_id,
                    fp.embedding_model,
                    len(fp.embedding),
                    _vec(fp.embedding),
                ),
            ).fetchall()
            best: DuplicateMatch | None = None
            for other_job, other_channel, sig, emb in rows:
                sh = jaccard_estimate(fp.minhash, list(sig or []))
                top = max(top, sh)
                action = classify_script(sh, float(emb), thresholds)
                if action and (best is None or max(sh, float(emb)) > best.score):
                    best = DuplicateMatch(
                        "script",
                        action,
                        max(sh, float(emb)),
                        other_job,
                        other_channel,
                        sh,
                        float(emb),
                    )
            if best:
                out.append(best)
            for name, digest in fp.media.items():
                hit = conn.execute(
                    "SELECT job_id::text, channel_id FROM content_fingerprints "
                    "WHERE kind = 'media' AND key = %s AND channel_id <> %s LIMIT 1",
                    (digest, channel_id),
                ).fetchone()
                if hit:
                    out.append(DuplicateMatch("media", "block", 1.0, hit[0], hit[1], label=name))
            for kind, key, esc, blk in _count_specs(fp, thresholds):
                if not key:
                    continue
                row = conn.execute(
                    "SELECT count(DISTINCT job_id), min(job_id::text), min(channel_id) "
                    "FROM content_fingerprints WHERE kind = %s AND key = %s AND channel_id <> %s "
                    "AND created_at > now() - %s::interval",
                    (kind, key, channel_id, window),
                ).fetchone()
                assert row is not None
                count = int(row[0])
                action = classify_count(count, esc, blk)
                if action:
                    out.append(
                        DuplicateMatch(kind, action, float(count), row[1], row[2], count=count)
                    )
        return DuplicateReport(out, top)


__all__ = [
    "DuplicateMatch",
    "DuplicateReport",
    "FingerprintIndex",
    "Fingerprints",
    "InMemoryFingerprintIndex",
    "PostgresFingerprintIndex",
]

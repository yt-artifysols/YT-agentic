from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class KnowledgeKind(StrEnum):
    BRAND_VOICE = "brand_voice"
    OFFERS = "offers"
    PLAYBOOK = "playbook"
    HOOK_LIBRARY = "hook_library"
    BANNED_CLAIMS = "banned_claims"
    STYLE_GUIDE = "style_guide"
    POLICY = "policy"  # JSON: per-channel production policy


@dataclass(frozen=True)
class KnowledgeDoc:
    id: str
    channel_id: str
    kind: KnowledgeKind
    version: int
    content: str
    content_sha256: str
    status: str  # draft | approved | rejected
    proposed_by: str
    base_version: int | None = None
    proposed_at: datetime | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejected_by: str | None = None
    reject_reason: str | None = None


def _lines(text: str) -> list[str]:
    return [
        line.strip().lstrip("-*").strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


@dataclass(frozen=True)
class KnowledgeSnapshot:
    """The approved knowledge an agent may use: one version per kind, one channel."""

    channel_id: str
    docs: Mapping[KnowledgeKind, KnowledgeDoc] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for doc in self.docs.values():
            if doc.channel_id != self.channel_id or doc.status != "approved":
                raise ValueError("a snapshot holds only approved docs of its own channel")

    def versions(self) -> dict[str, int]:
        return {str(kind): doc.version for kind, doc in sorted(self.docs.items())}

    def doc_ids(self) -> list[str]:
        return [doc.id for _, doc in sorted(self.docs.items())]

    def text(self, kind: KnowledgeKind) -> str:
        doc = self.docs.get(kind)
        return doc.content if doc else ""

    def lines(self, kind: KnowledgeKind) -> list[str]:
        return _lines(self.text(kind))

    @property
    def banned_claims(self) -> list[str]:
        return self.lines(KnowledgeKind.BANNED_CLAIMS)

    @property
    def hooks(self) -> list[str]:
        return self.lines(KnowledgeKind.HOOK_LIBRARY)

    @property
    def policy(self) -> dict[str, Any]:
        raw = self.text(KnowledgeKind.POLICY)
        if not raw:
            return {}
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("policy knowledge must be a JSON object")
        return value

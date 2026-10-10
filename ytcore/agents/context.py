"""Per-channel context: brand profile, policy and memory, loaded for ONE channel.

Agents receive a ChannelContext and nothing else about the channel. Knowledge comes from the
versions pinned for the job (pinned from the latest approved versions when research starts),
so every stage of a job sees the same approved knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ytcore.agents.memory import ChannelMemory
from ytcore.knowledge.models import KnowledgeKind, KnowledgeSnapshot
from ytcore.knowledge.store import ChannelKnowledge


class ChannelPolicy(BaseModel):
    """Per-channel production policy (the approved `policy` knowledge doc, JSON)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: str = "en"
    category_id: str = "22"
    made_for_kids: bool = False
    max_segments: int = Field(default=8, ge=1, le=40)
    min_sources: int = Field(default=1, ge=0, le=10)
    max_sources: int = Field(default=5, ge=1, le=10)
    max_risk_score: int = Field(default=40, ge=0, le=100)
    min_brand_voice_score: float = Field(default=0.5, ge=0, le=1)
    voice: str = "narrator-1"
    tone_words: list[str] = Field(default_factory=list)
    forbidden_words: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class BrandProfile:
    voice: str
    style_guide: str
    offers: str
    playbook: str
    hooks: list[str]
    banned_claims: list[str]


@dataclass(frozen=True)
class ChannelProfile:
    channel_id: str
    storage_prefix: str
    memory_namespace: str
    autonomy_tier: str


@dataclass(frozen=True)
class ChannelContext:
    channel_id: str
    storage_prefix: str
    autonomy_tier: str
    knowledge: KnowledgeSnapshot
    brand: BrandProfile
    policy: ChannelPolicy
    memory: ChannelMemory

    def guidance(self) -> str:
        """Human-approved channel guidance, safe to place in a system prompt."""
        b = self.brand
        parts = [
            ("Brand voice", b.voice),
            ("Style guide", b.style_guide),
            ("Offers", b.offers),
            (
                "Banned claims (never state or imply these)",
                "\n".join(f"- {c}" for c in b.banned_claims),
            ),
        ]
        return "\n\n".join(f"## {title}\n{text}" for title, text in parts if text.strip())


class ChannelDirectory(Protocol):
    def channel_profile(self, channel_id: str) -> ChannelProfile: ...


class KnowledgeFactory(Protocol):
    def channel(self, channel_id: str) -> ChannelKnowledge: ...


class MemoryFactory(Protocol):
    def for_channel(self, channel_id: str) -> ChannelMemory: ...


def brand_from(snapshot: KnowledgeSnapshot) -> BrandProfile:
    return BrandProfile(
        voice=snapshot.text(KnowledgeKind.BRAND_VOICE),
        style_guide=snapshot.text(KnowledgeKind.STYLE_GUIDE),
        offers=snapshot.text(KnowledgeKind.OFFERS),
        playbook=snapshot.text(KnowledgeKind.PLAYBOOK),
        hooks=snapshot.hooks,
        banned_claims=snapshot.banned_claims,
    )


def policy_from(snapshot: KnowledgeSnapshot) -> ChannelPolicy:
    raw: dict[str, Any] = snapshot.policy
    return ChannelPolicy.model_validate(raw)


class ChannelContextLoader:
    def __init__(
        self, directory: ChannelDirectory, knowledge: KnowledgeFactory, memory: MemoryFactory
    ) -> None:
        self._directory, self._knowledge, self._memory = directory, knowledge, memory

    def knowledge(self, channel_id: str) -> ChannelKnowledge:
        return self._knowledge.channel(channel_id)

    def load(self, job_id: str, channel_id: str, *, pin_latest: bool) -> ChannelContext:
        profile = self._directory.channel_profile(channel_id)
        kb = self._knowledge.channel(channel_id)
        if pin_latest:
            kb.pin(job_id, kb.latest_approved(), actor="orchestrator")
        snapshot = kb.pinned(job_id)
        return ChannelContext(
            channel_id=channel_id,
            storage_prefix=profile.storage_prefix,
            autonomy_tier=profile.autonomy_tier,
            knowledge=snapshot,
            brand=brand_from(snapshot),
            policy=policy_from(snapshot),
            memory=self._memory.for_channel(channel_id),
        )

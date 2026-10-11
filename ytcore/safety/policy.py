"""Per-channel safety policy: the `safety` section of the channel's approved `policy` knowledge
document. Every threshold is configurable; defaults are conservative."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SensitiveTopic = Literal["health", "finance", "politics", "minors", "breaking_news"]
SENSITIVE_TOPICS: tuple[SensitiveTopic, ...] = (
    "health",
    "finance",
    "politics",
    "minors",
    "breaking_news",
)


class Handling(StrEnum):
    ALLOW = "allow"  # no extra risk for this topic on this channel
    ESCALATE = "escalate"  # adds risk and a flag so a human looks closely
    BLOCK = "block"  # QA fails; the script is reworked or the job fails


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DuplicateThresholds(_Strict):
    """Cross-channel duplicate detection. Similarities are 0..1; counts are other-channel jobs
    in the last `window_days`."""

    shingle_escalate: float = Field(default=0.35, ge=0, le=1)
    shingle_block: float = Field(default=0.6, ge=0, le=1)
    embedding_escalate: float = Field(default=0.9, ge=0, le=1)
    embedding_block: float = Field(default=0.97, ge=0, le=1)
    title_template_escalate: int = Field(default=3, ge=1)
    title_template_block: int = Field(default=8, ge=1)
    thumbnail_text_escalate: int = Field(default=3, ge=1)
    thumbnail_text_block: int = Field(default=8, ge=1)
    window_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def _ordered(self) -> DuplicateThresholds:
        pairs = [
            (self.shingle_escalate, self.shingle_block),
            (self.embedding_escalate, self.embedding_block),
            (self.title_template_escalate, self.title_template_block),
            (self.thumbnail_text_escalate, self.thumbnail_text_block),
        ]
        if any(low > high for low, high in pairs):
            raise ValueError("escalate thresholds must not exceed block thresholds")
        return self


def _default_sensitive() -> dict[SensitiveTopic, Handling]:
    return dict.fromkeys(SENSITIVE_TOPICS, Handling.ESCALATE)


class SafetyPolicy(_Strict):
    # Words or phrases that must never appear in a video of this channel (case-insensitive).
    banned_topics: list[str] = Field(default_factory=list, max_length=200)
    sensitive: dict[SensitiveTopic, Handling] = Field(default_factory=_default_sensitive)
    # Risk score (0..1) at or above which a human is warned / QA fails.
    escalate_risk_at: float = Field(default=0.35, ge=0, le=1)
    block_risk_at: float = Field(default=0.8, ge=0, le=1)
    # sha256 digests of media that must never be used (known third-party content).
    blocked_media_sha256: list[str] = Field(default_factory=list, max_length=10_000)
    duplicates: DuplicateThresholds = Field(default_factory=DuplicateThresholds)
    # Same-channel repetition: share of repeated sentences inside one script.
    max_repeated_sentence_ratio: float = Field(default=0.3, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> SafetyPolicy:
        if self.escalate_risk_at > self.block_risk_at:
            raise ValueError("escalate_risk_at must not exceed block_risk_at")
        return self

    def handling(self, topic: SensitiveTopic) -> Handling:
        return self.sensitive.get(topic, Handling.ESCALATE)

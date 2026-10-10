"""Typed agent inputs and outputs. Strict: unknown fields are rejected."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FootageRef(Strict):
    input_id: str
    uri: str
    sha256: str
    note: str = ""


class Brief(Strict):
    job_id: str
    channel_id: str
    origin: str = "planner"
    topic: str = Field(min_length=1, max_length=500)
    angle: str = Field(default="", max_length=500)
    notes: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    footage: list[FootageRef] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list, max_length=10)
    owner_input_ids: list[str] = Field(default_factory=list)
    goal_input_ids: list[str] = Field(default_factory=list)


class Source(Strict):
    id: str = Field(pattern=r"^S\d{1,2}$")
    url: str
    title: str = Field(max_length=300)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    excerpt: str = Field(max_length=4000)


class Fact(Strict):
    id: str = Field(pattern=r"^F\d{1,3}$")
    claim: str = Field(min_length=1, max_length=400)
    source_ids: list[str] = Field(min_length=1)


class ResearchOutput(Strict):
    brief: Brief
    sources: list[Source]
    facts: list[Fact]
    angle: str = Field(default="", max_length=500)
    memory_notes: list[str] = Field(default_factory=list)
    injection_suspected: list[str] = Field(default_factory=list)  # source ids
    prompt_version: str
    model: str


class Segment(Strict):
    text: str = Field(min_length=1, max_length=2000)
    fact_ids: list[str] = Field(default_factory=list)


class ScriptOutput(Strict):
    title: str = Field(min_length=1, max_length=100)
    hook: str = Field(min_length=1, max_length=400)
    segments: list[Segment] = Field(min_length=1, max_length=40)
    cta: str = Field(default="", max_length=400)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    revision_notes: list[str] = Field(default_factory=list)
    prompt_version: str
    model: str

    def full_text(self) -> str:
        return "\n\n".join([self.hook, *(s.text for s in self.segments), self.cta]).strip()


AssetKind = Literal["video", "audio", "music", "stock", "image", "document"]


class AssetRef(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,62}$")
    kind: AssetKind
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    media_type: str
    provider: str
    license_ref: str | None = None
    version: int = Field(ge=1)


class MediaOutput(Strict):
    assets: list[AssetRef]
    prompt_version: str
    model: str


Severity = Literal["info", "warn", "block"]


class QAFlag(Strict):
    code: str
    severity: Severity
    detail: str = Field(max_length=500)
    evidence: str = Field(default="", max_length=400)


class QAOutput(Strict):
    passed: bool
    risk_score: int = Field(ge=0, le=100)
    flags: list[QAFlag]
    rework_stage: Literal["scripting", "rendering"] | None = None
    brand_voice_score: float = Field(ge=0, le=1)
    prompt_version: str
    model: str

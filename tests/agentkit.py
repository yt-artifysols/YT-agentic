"""Wiring for agent pipeline tests with in-memory stores and fake providers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ytcore.agents.fake import (
    FakeImage,
    FakeLLM,
    FakeStockLibrary,
    FakeTTS,
    FakeVideo,
    FakeWebFetcher,
)
from ytcore.agents.inmemory import InMemoryProductionStore
from ytcore.agents.memory import InMemoryMemory
from ytcore.agents.pipeline import AgentPipeline, Providers
from ytcore.approvals.hashing import content_hash
from ytcore.gateway.assets import InMemoryAssetSource
from ytcore.inputs.models import InputKind
from ytcore.inputs.store import InMemoryInputs
from ytcore.knowledge.models import KnowledgeKind
from ytcore.knowledge.store import InMemoryKnowledge
from ytcore.workflow.models import QAReport

BRAND_VOICE = "Calm, precise and friendly. Plain words, no hype."
HOOKS = "Most people get {topic} wrong.\nHere is what the evidence says about {topic}."
BANNED = "guaranteed returns\ncures any disease"
POLICY = {"min_sources": 1, "tone_words": ["evidence"], "forbidden_words": ["hype"]}
SOURCE_URL = "https://example.test/index-funds"
SOURCE_TEXT = (
    "Index funds track a market index and charge low fees. "
    "Over twenty years most active funds trailed their benchmark index. "
    "Diversification lowers the risk of a single company failing."
)


@dataclass
class Kit:
    store: InMemoryProductionStore = field(default_factory=InMemoryProductionStore)
    knowledge: InMemoryKnowledge = field(default_factory=InMemoryKnowledge)
    inputs: InMemoryInputs = field(default_factory=InMemoryInputs)
    memory: InMemoryMemory = field(default_factory=InMemoryMemory)
    assets: InMemoryAssetSource = field(default_factory=InMemoryAssetSource)
    llm: FakeLLM = field(default_factory=FakeLLM)
    web: FakeWebFetcher = field(default_factory=FakeWebFetcher)
    stock: FakeStockLibrary = field(default_factory=FakeStockLibrary)
    pipeline: AgentPipeline | None = None
    jobs: int = 0

    def __post_init__(self) -> None:
        self.web.pages.setdefault(SOURCE_URL, ("Index funds explained", SOURCE_TEXT))
        self.pipeline = AgentPipeline(
            store=self.store,
            knowledge=self.knowledge,
            inputs=self.inputs,
            memory=self.memory,
            assets=self.assets,
            providers=Providers(
                self.llm, FakeTTS(), FakeImage(), FakeVideo(), self.stock, self.web
            ),
        )

    @property
    def p(self) -> AgentPipeline:
        assert self.pipeline is not None
        return self.pipeline

    def channel(
        self,
        channel_id: str = "chan-a",
        *,
        seed: bool = True,
        safety: dict[str, Any] | None = None,
        **kw: Any,
    ) -> str:
        self.store.add_channel(channel_id, **kw)
        policy = POLICY | ({"safety": safety} if safety else {})
        if seed:
            for kind, content in (
                (KnowledgeKind.BRAND_VOICE, BRAND_VOICE),
                (KnowledgeKind.HOOK_LIBRARY, HOOKS),
                (KnowledgeKind.BANNED_CLAIMS, BANNED),
                (KnowledgeKind.POLICY, json.dumps(policy)),
            ):
                self.knowledge.seed_approved(channel_id, kind, content)
        return channel_id

    def idea(self, channel_id: str, text: str, *, url: str = SOURCE_URL) -> str:
        body = f"{text}\nSources: {url}" if url else text
        return self.inputs.add(channel_id, InputKind.IDEA, body=body, submitted_by="owner").id

    def job(self, channel_id: str) -> str:
        self.jobs += 1
        job_id = f"00000000-0000-0000-0000-{self.jobs:012d}"
        self.store.add_job(job_id, channel_id)
        return job_id

    def produce(self, job_id: str, channel_id: str, *, max_rework: int = 2) -> Result:
        """Run research -> script -> render -> QA (with QA rework) like the workflow does."""
        artifacts: dict[str, str] = {}
        for stage in ("research", "script", "render"):
            artifacts |= self.p.run_stage(job_id, channel_id, stage, artifacts)
        reworks = 0
        report = self.p.run_qa(job_id, channel_id, artifacts)
        while not report.passed and reworks < max_rework:
            reworks += 1
            if report.rework_stage == "scripting":
                artifacts |= self.p.run_stage(job_id, channel_id, "script", artifacts)
            artifacts |= self.p.run_stage(job_id, channel_id, "render", artifacts)
            report = self.p.run_qa(job_id, channel_id, artifacts)
        manifest = self.p.build_manifest(job_id, channel_id, artifacts) if report.passed else {}
        return Result(artifacts, report, manifest, reworks)


@dataclass
class Result:
    artifacts: dict[str, str]
    qa: QAReport
    manifest: dict[str, Any]
    reworks: int

    @property
    def content_hash(self) -> str:
        return content_hash(self.manifest)

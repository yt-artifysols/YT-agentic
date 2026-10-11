"""The agent pipeline behind JobActivities: research -> script_seo -> media -> qa -> manifest.

Stage outputs are stored as versioned, content-addressed documents under the channel's storage
prefix and passed between Temporal activities by digest. Every stage reloads the channel
context for the job: knowledge pinned for the job, the channel's policy and its own memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from ytcore.agents.context import (
    ChannelContext,
    ChannelContextLoader,
    KnowledgeFactory,
    MemoryFactory,
)
from ytcore.agents.costs import CostMeter
from ytcore.agents.manifest import build_manifest
from ytcore.agents.media import MediaAgent
from ytcore.agents.memory import Embedder, HashEmbedder
from ytcore.agents.models import AssetRef, MediaOutput, ResearchOutput, ScriptOutput
from ytcore.agents.providers import (
    ImageProvider,
    LLMProvider,
    MediaResult,
    StockItem,
    StockLibrary,
    TTSProvider,
    VideoProvider,
    WebFetcher,
)
from ytcore.agents.qa import QAAgent, SafetyDeps
from ytcore.agents.research import ResearchAgent
from ytcore.agents.script_seo import ScriptSeoAgent
from ytcore.agents.store import AssetStore, ProductionStore
from ytcore.agents.tools import Tool, ToolBox
from ytcore.approvals.hashing import file_digest
from ytcore.gateway.assets import read_verified
from ytcore.inputs.store import InputStore
from ytcore.observability.tracing import job_span
from ytcore.planner.brief import BriefGenerator
from ytcore.safety.assets import AssetLimits, check_asset
from ytcore.safety.duplicates import FingerprintIndex, InMemoryFingerprintIndex
from ytcore.workflow.models import QAReport

M = TypeVar("M", bound=BaseModel)

RESEARCH, SCRIPT, MEDIA, QA = "research.json", "script.json", "media.json", "qa.json"


@dataclass(frozen=True)
class Providers:
    llm: LLMProvider
    tts: TTSProvider
    image: ImageProvider
    video: VideoProvider
    stock: StockLibrary
    web: WebFetcher


class AgentPipeline:
    def __init__(
        self,
        *,
        store: ProductionStore,
        knowledge: KnowledgeFactory,
        inputs: InputStore,
        memory: MemoryFactory,
        assets: AssetStore,
        providers: Providers,
        fingerprints: FingerprintIndex | None = None,
        embedder: Embedder | None = None,
        asset_limits: AssetLimits | None = None,
    ) -> None:
        self._store, self._assets, self._p = store, assets, providers
        self._embedder = embedder or HashEmbedder()
        # Without a shared index (tests, evals) duplicates are only found within this pipeline.
        self._fingerprints = fingerprints or InMemoryFingerprintIndex()
        self._limits = asset_limits or AssetLimits.default()
        self._loader = ChannelContextLoader(store, knowledge, memory)
        self._briefs = BriefGenerator(inputs)
        self.research = ResearchAgent(providers.llm)
        self.script = ScriptSeoAgent(providers.llm)
        self.media = MediaAgent(providers.llm)
        self.qa = QAAgent(providers.llm)
        self.toolboxes: list[ToolBox] = []  # every toolbox handed out (for tests and audits)

    # ------------------------------------------------------------------ helpers

    def _meter(self, job_id: str, channel_id: str, agent: str) -> CostMeter:
        return CostMeter(self._store, job_id, channel_id, actor=f"agent:{agent}")

    def _put(
        self,
        job_id: str,
        ctx: ChannelContext,
        name: str,
        kind: str,
        data: bytes,
        media_type: str,
        provider: str,
        license_ref: str | None = None,
    ) -> AssetRef:
        check_asset(kind, media_type, data, self._limits)  # size, type and signature limits
        digest = self._assets.put(ctx.storage_prefix, data)
        version = self._store.record_asset(
            job_id,
            ctx.channel_id,
            name=name,
            kind=kind,
            sha256=digest,
            media_type=media_type,
            provider=provider,
            license_ref=license_ref,
        )
        return AssetRef(
            name=name,
            kind=kind,
            sha256=digest,
            media_type=media_type,
            provider=provider,
            license_ref=license_ref,
            version=version,
        )

    def _save(self, job_id: str, ctx: ChannelContext, name: str, doc: BaseModel) -> str:
        data = doc.model_dump_json().encode()
        return self._put(job_id, ctx, name, "document", data, "application/json", "pipeline").sha256

    def _load(
        self, ctx: ChannelContext, artifacts: dict[str, str], name: str, schema: type[M]
    ) -> M:
        if name not in artifacts:
            raise KeyError(f"missing stage output {name}")
        return schema.model_validate_json(
            read_verified(self._assets, ctx.storage_prefix, artifacts[name])
        )

    def _toolbox(self, agent: str, impls: dict[Tool, Any]) -> ToolBox:
        box = ToolBox.for_agent(agent, impls)
        self.toolboxes.append(box)
        return box

    def _media_tools(self, job_id: str, ctx: ChannelContext, meter: CostMeter) -> dict[Tool, Any]:
        p = self._p

        def metered(kind: str, estimate: int, call: Any) -> MediaResult:
            meter.authorize(estimate, kind)
            res: MediaResult = call()
            meter.charge("media", res.cost_cents, provider=res.provider, what=kind)
            return res

        def stock(query: str, *, kind: str) -> tuple[StockItem, MediaResult]:
            items = p.stock.search(query, kind=kind, limit=3)
            usable = [i for i in items if self._store.license_valid(i.license_ref, ctx.channel_id)]
            if not usable:
                raise LookupError(f"no licensed {kind} for {query!r}")
            item = usable[0]
            res = metered(
                f"stock:{kind}", p.stock.estimate_cents(item), lambda: p.stock.fetch(item)
            )
            return item, res

        return {
            Tool.TTS: lambda text, voice: metered(
                "tts", p.tts.estimate_cents(text), lambda: p.tts.synthesize(text, voice=voice)
            ),
            Tool.IMAGE: lambda prompt: metered(
                "image", p.image.estimate_cents(prompt), lambda: p.image.generate(prompt)
            ),
            Tool.VIDEO: lambda timeline, inputs: metered(
                "video",
                p.video.estimate_cents(timeline),
                lambda: p.video.assemble(timeline, inputs),
            ),
            Tool.STOCK_SEARCH: stock,
            Tool.STORAGE_WRITE: lambda name, kind, data, media_type, provider, license_ref=None: (
                self._put(job_id, ctx, name, kind, data, media_type, provider, license_ref)
            ),
        }

    def _qa_tools(self, ctx: ChannelContext) -> dict[Tool, Any]:
        def read(asset: AssetRef) -> bytes:
            data = read_verified(self._assets, ctx.storage_prefix, asset.sha256)
            if file_digest(data) != asset.sha256:  # pragma: no cover - read_verified checks
                raise ValueError("digest mismatch")
            return data

        return {
            Tool.STORAGE_READ: read,
            Tool.RIGHTS_READ: lambda ref: self._store.license_valid(ref, ctx.channel_id),
        }

    # ------------------------------------------------------------------ stages

    def run_stage(
        self, job_id: str, channel_id: str, stage: str, artifacts: dict[str, str]
    ) -> dict[str, str]:
        with job_span(f"agent.{stage}", job_id, channel_id, stage=stage):
            return self._run_stage(job_id, channel_id, stage, artifacts)

    def _run_stage(
        self, job_id: str, channel_id: str, stage: str, artifacts: dict[str, str]
    ) -> dict[str, str]:
        if stage == "research":
            ctx = self._loader.load(job_id, channel_id, pin_latest=True)
            brief = self._briefs.generate(job_id, channel_id, ctx.knowledge, origin="planner")
            tools = self._toolbox("research", {Tool.WEB_FETCH: self._p.web.fetch})
            out = self.research.run(brief, ctx, tools, self._meter(job_id, channel_id, "research"))
            ctx.memory.add("research_note", f"{brief.topic}: {out.angle}"[:2000], source_job=job_id)
            return {RESEARCH: self._save(job_id, ctx, RESEARCH, out)}

        ctx = self._loader.load(job_id, channel_id, pin_latest=False)
        research = self._load(ctx, artifacts, RESEARCH, ResearchOutput)
        if stage == "script":
            notes = self._store.revision_notes(job_id)
            script = self.script.run(
                research, ctx, self._meter(job_id, channel_id, "script_seo"), revision_notes=notes
            )
            return {SCRIPT: self._save(job_id, ctx, SCRIPT, script)}
        if stage == "render":
            script = self._load(ctx, artifacts, SCRIPT, ScriptOutput)
            meter = self._meter(job_id, channel_id, "media")
            tools = self._toolbox("media", self._media_tools(job_id, ctx, meter))
            media = self.media.run(script, research, ctx, tools, meter)
            return {MEDIA: self._save(job_id, ctx, MEDIA, media)} | {
                a.name: a.sha256 for a in media.assets
            }
        raise ValueError(f"unknown stage {stage!r}")

    def run_qa(self, job_id: str, channel_id: str, artifacts: dict[str, str]) -> QAReport:
        with job_span("agent.qa", job_id, channel_id, stage="qa") as span:
            report = self._run_qa(job_id, channel_id, artifacts)
            span.set_attribute("qa.passed", report.passed)
            span.set_attribute("qa.risk_score", report.risk_score)
            return report

    def _run_qa(self, job_id: str, channel_id: str, artifacts: dict[str, str]) -> QAReport:
        ctx = self._loader.load(job_id, channel_id, pin_latest=False)
        research = self._load(ctx, artifacts, RESEARCH, ResearchOutput)
        script = self._load(ctx, artifacts, SCRIPT, ScriptOutput)
        media = self._load(ctx, artifacts, MEDIA, MediaOutput)
        tools = self._toolbox("qa", self._qa_tools(ctx))
        out = self.qa.run(
            research,
            script,
            media,
            ctx,
            tools,
            self._meter(job_id, channel_id, "qa"),
            SafetyDeps(job_id, self._fingerprints, self._embedder),
        )
        self._save(job_id, ctx, QA, out)
        self._store.record_risk(job_id, channel_id, out.risk)
        self._store.record_qa_flags(job_id, [f.detail for f in out.flags if f.severity == "block"])
        return QAReport(
            passed=out.passed,
            rework_stage=out.rework_stage,
            notes=f"brand voice {out.brand_voice_score:.2f}",
            risk_score=out.risk_score,
            flags=[
                {
                    "code": f.code,
                    "severity": f.severity,
                    "claim": f.evidence or f.detail,
                    "reason": f.detail,
                }
                for f in out.flags
                if f.severity != "info"
            ],
            sources=[{"id": s.id, "url": s.url, "title": s.title} for s in research.sources],
        )

    def build_manifest(
        self, job_id: str, channel_id: str, artifacts: dict[str, str]
    ) -> dict[str, Any]:
        ctx = self._loader.load(job_id, channel_id, pin_latest=False)
        research = self._load(ctx, artifacts, RESEARCH, ResearchOutput)
        script = self._load(ctx, artifacts, SCRIPT, ScriptOutput)
        media = self._load(ctx, artifacts, MEDIA, MediaOutput)
        # Remember this script so later videos of the channel are checked for repetition.
        ctx.memory.add("script", script.full_text(), source_job=job_id)
        return build_manifest(
            job_id=job_id,
            channel_id=channel_id,
            research=research,
            script=script,
            media=media,
            documents={k: artifacts[k] for k in (RESEARCH, SCRIPT, MEDIA)},
            knowledge=ctx.knowledge,
            policy=ctx.policy,
        )

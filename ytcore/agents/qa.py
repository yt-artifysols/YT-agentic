"""QA pool. Skills: brand checker (model), policy check (banned claims, forbidden words),
fact check (citations and support), license check (rights register), asset integrity,
injection check. Tools: read-only storage and the rights register.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import Field

from ytcore.agents.base import Agent, Reply
from ytcore.agents.context import ChannelContext
from ytcore.agents.costs import CostMeter
from ytcore.agents.memory import Embedder
from ytcore.agents.models import (
    MediaOutput,
    QAFlag,
    QAOutput,
    ResearchOutput,
    ScriptOutput,
    Severity,
)
from ytcore.agents.tools import Tool, ToolBox
from ytcore.agents.untrusted import injection_signals
from ytcore.safety import risk as risk_mod
from ytcore.safety.checks import GENERATED_KINDS, AssetView, CheckInput, Finding, check_content
from ytcore.safety.duplicates import (
    DuplicateMatch,
    FingerprintIndex,
    Fingerprints,
    text_similarity,
)

REQUIRED_ASSETS = ("video", "thumbnail", "voiceover")
_STOP = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was",
        "were", "be", "by", "with", "that", "this", "it", "as", "at", "from", "about", "into",
        "than", "then", "so",
    }
)  # fmt: skip


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP}


def _overlap(claim: str, text: str) -> float:
    c = _tokens(claim)
    return len(c & _tokens(text)) / len(c) if c else 1.0


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


class _Brand(Reply):
    brand_voice_score: float = Field(ge=0, le=1)
    notes: str = Field(default="", max_length=500)


class _Policy(Reply):
    misleading: bool
    reason: str = Field(default="", max_length=300)


@dataclass(frozen=True)
class SafetyDeps:
    """Platform services QA code (never a model) uses: the cross-channel fingerprint index."""

    job_id: str
    fingerprints: FingerprintIndex
    embedder: Embedder


DUPLICATE_CODES = {
    "script": ("duplicate_script", "scripting"),
    "media": ("reused_media", "rendering"),
    "title_template": ("repeated_title_template", "scripting"),
    "thumbnail_text": ("repeated_thumbnail_text", "rendering"),
}


class QAAgent(Agent):
    name = "qa"

    def run(
        self,
        research: ResearchOutput,
        script: ScriptOutput,
        media: MediaOutput,
        ctx: ChannelContext,
        tools: ToolBox,
        meter: CostMeter,
        safety: SafetyDeps,
    ) -> QAOutput:
        flags: list[QAFlag] = []
        script_stage, render_stage = False, False

        def flag(code: str, severity: Severity, detail: str, evidence: str = "") -> None:
            flags.append(
                QAFlag(code=code, severity=severity, detail=detail[:500], evidence=evidence[:400])
            )

        text = script.full_text()
        sentences = _sentences(f"{script.title}. {text}\n{script.description}")

        # Policy: banned claims and forbidden words (human-approved knowledge).
        for claim in ctx.brand.banned_claims:
            for s in sentences:
                if claim.lower() in s.lower() or _overlap(claim, s) >= 0.8:
                    flag("banned_claim", "block", f"avoid: {claim}", s)
                    script_stage = True
                    break
        for word in ctx.policy.forbidden_words:
            if word and re.search(rf"\b{re.escape(word.lower())}\b", text.lower()):
                flag("forbidden_word", "block", f"avoid: {word}", word)
                script_stage = True

        # Facts: every segment cites known facts and says what they say.
        facts = {f.id: f for f in research.facts}
        sources = {s.id for s in research.sources}
        for seg in script.segments:
            cited = [facts[i] for i in seg.fact_ids if i in facts]
            if not cited:
                if re.search(r"\d", seg.text):
                    flag(
                        "uncited_claim",
                        "warn",
                        "segment states figures without a fact id",
                        seg.text,
                    )
                continue
            if _overlap(seg.text, " ".join(f.claim for f in cited)) < 0.6:
                flag("unsupported_claim", "warn", "segment goes beyond its cited facts", seg.text)
        for f in research.facts:
            if not set(f.source_ids) <= sources:
                flag("unknown_source", "block", f"fact {f.id} cites an unknown source", f.claim)
                script_stage = True
        if len(research.sources) < ctx.policy.min_sources:
            flag(
                "insufficient_sources",
                "warn",
                f"{len(research.sources)} source(s), policy wants {ctx.policy.min_sources}",
            )

        # Untrusted content: instructions inside sources were ignored; none may leak into output.
        if research.injection_suspected:
            flag(
                "untrusted_instructions_ignored",
                "warn",
                "sources contained instruction-like text; it was treated as data",
                ", ".join(research.injection_suspected),
            )
        leaked = injection_signals(f"{script.title}\n{text}\n{script.description}")
        if leaked:
            flag(
                "instructions_in_output",
                "block",
                "script contains instruction-like text",
                leaked[0],
            )
            script_stage = True

        # Assets: required set, integrity, licenses.
        by_name = {a.name: a for a in media.assets}
        for name in REQUIRED_ASSETS:
            if name not in by_name:
                flag("missing_asset", "block", f"missing {name}")
                render_stage = True
        for asset in media.assets:
            try:
                tools.call(Tool.STORAGE_READ, asset)
            except (LookupError, ValueError, OSError) as exc:
                flag("asset_integrity", "block", f"{asset.name}: {exc}")
                render_stage = True

        # Policy rules (docs/policy-sources.md): deterministic first.
        policy = ctx.policy.safety
        history = [
            m
            for m in ctx.memory.search(text, k=5)
            if m.kind == "script" and m.source_job != safety.job_id
        ]
        history_similarity = max((text_similarity(text, m.content) for m in history), default=0.0)
        result = check_content(
            CheckInput(
                topic=research.brief.topic,
                title=script.title,
                script_text=text,
                description=script.description,
                tags=tuple(script.tags),
                thumbnail_text=media.thumbnail_text,
                assets=tuple(
                    AssetView(a.name, a.kind, a.sha256, a.license_ref, a.provider)
                    for a in media.assets
                ),
                safety=policy,
                license_ok=lambda ref: bool(tools.call(Tool.RIGHTS_READ, ref)),
                history_similarity=history_similarity,
            )
        )
        stages: set[str] = set()
        for finding in result.findings:
            self._add(flags, finding)
            if finding.severity == "block" and finding.stage:
                stages.add(finding.stage)
        if "scripting" in stages:
            script_stage = True
        if "rendering" in stages:
            render_stage = True

        # The model is asked only about the borderline title/thumbnail cases rules cannot settle.
        if result.borderline:
            verdict = self.ask(
                "policy",
                _Policy,
                meter=meter,
                guidance=ctx.guidance(),
                max_tokens=200,
                task="Could the title or thumbnail text mislead viewers about this video?",
                data=[
                    ("title", script.title),
                    ("thumbnail text", media.thumbnail_text),
                    ("script excerpt", text[:1500]),
                ],
            )
            if verdict.misleading:
                for label in result.borderline:
                    flag(
                        f"misleading_{label}",
                        "warn",
                        f"the {label} may mislead viewers (model judgment)",
                        verdict.reason,
                    )

        # Cross-channel duplicates: code-only; match details go to human approvers as evidence.
        generated = {a.name: a.sha256 for a in media.assets if a.kind in GENERATED_KINDS}
        fingerprints = Fingerprints.build(
            script_text=text,
            title=script.title,
            thumbnail_text=media.thumbnail_text,
            media=generated,
            embedder=safety.embedder,
        )
        # Record BEFORE looking: of two concurrent near-identical jobs, at least the later one
        # sees the other (find only returns other channels' rows).
        safety.fingerprints.record(job_id=safety.job_id, channel_id=ctx.channel_id, fp=fingerprints)
        report = safety.fingerprints.find(
            job_id=safety.job_id,
            channel_id=ctx.channel_id,
            fp=fingerprints,
            thresholds=policy.duplicates,
        )
        for match in report.matches:
            code, dup_stage = DUPLICATE_CODES[match.kind]
            severity: Severity = "block" if match.action == "block" else "warn"
            flag(code, severity, self._duplicate_detail(match), match.evidence())
            if severity == "block":
                script_stage |= dup_stage == "scripting"
                render_stage |= dup_stage == "rendering"
        flag("synthetic_media_disclosed", "info", "AI-produced; disclosed as synthetic media")

        # Brand voice (model-judged against the approved brand voice).
        brand = self.ask(
            "brand",
            _Brand,
            meter=meter,
            guidance=ctx.guidance(),
            max_tokens=200,
            task="Score the script against the approved brand voice.",
            data=[
                ("script", f"{script.title}\n\n{text}"),
                ("tone words", "\n".join(ctx.policy.tone_words)),
                ("forbidden words", "\n".join(ctx.policy.forbidden_words)),
            ],
        )
        if brand.brand_voice_score < ctx.policy.min_brand_voice_score:
            flag(
                "brand_voice",
                "warn",
                f"brand voice score {brand.brand_voice_score:.2f}",
                brand.notes,
            )

        # Risk score: 0..1 with named reasons, stored on the job by the pipeline.
        cited_ids = [{i for i in seg.fact_ids if i in facts} for seg in script.segments]
        assessment = risk_mod.assess(
            risk_mod.RiskInputs(
                total_claims=len(script.segments),
                unsourced_claims=sum(1 for c in cited_ids if not c),
                sensitive_topics=tuple(result.sensitive),
                rights=tuple(result.rights),
                brand_voice_score=brand.brand_voice_score,
                history_similarity=history_similarity,
                owner_inputs_used=len(research.brief.owner_input_ids),
                cross_channel_similarity=report.script_similarity,
                policy_warnings=sum(1 for f in flags if f.severity == "warn"),
            ),
            policy,
        )
        names = ", ".join(f"{r.code} {r.contribution:.2f}" for r in assessment.reasons[:4])
        if assessment.block:
            flag("risk_too_high", "block", f"avoid: too many risk signals ({names})", names)
            script_stage = True
        elif assessment.escalate:
            flag("risk_escalated", "warn", "risk is high; review closely", names)

        blocked = any(f.severity == "block" for f in flags)
        stage = "scripting" if script_stage else "rendering" if render_stage else None
        return QAOutput(
            passed=not blocked,
            risk_score=assessment.score_100,
            flags=flags,
            rework_stage=stage if blocked else None,
            brand_voice_score=brand.brand_voice_score,
            risk=assessment.to_json(),
            prompt_version=self.prompt.ref,
            model=self.llm.model,
        )

    @staticmethod
    def _add(flags: list[QAFlag], finding: Finding) -> None:
        flags.append(
            QAFlag(
                code=finding.code,
                severity=finding.severity,
                detail=finding.detail[:500],
                evidence=finding.evidence[:400],
            )
        )

    @staticmethod
    def _duplicate_detail(match: DuplicateMatch) -> str:
        # Generic on purpose: this text can reach the script writer as a revision note, so it
        # never names another channel or job.
        what = {
            "script": "the script is too similar to another channel's",
            "media": "generated media is identical to another channel's",
            "title_template": "the title format is used by many other channels",
            "thumbnail_text": "the thumbnail text is used by many other channels",
        }[match.kind]
        return f"avoid: {what}" if match.action == "block" else f"{what} (escalated)"

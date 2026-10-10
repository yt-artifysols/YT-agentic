"""QA pool. Skills: brand checker (model), policy check (banned claims, forbidden words),
fact check (citations and support), license check (rights register), asset integrity,
injection check. Tools: read-only storage and the rights register.
"""

from __future__ import annotations

import re

from pydantic import Field

from ytcore.agents.base import Agent, Reply
from ytcore.agents.context import ChannelContext
from ytcore.agents.costs import CostMeter
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


RISK = {"info": 0, "warn": 15, "block": 40}


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
            if asset.kind in ("music", "stock") and not (
                asset.license_ref and tools.call(Tool.RIGHTS_READ, asset.license_ref)
            ):
                flag(
                    "license_invalid",
                    "block",
                    f"{asset.name} has no valid license",
                    asset.license_ref or "",
                )
                render_stage = True
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

        blocked = any(f.severity == "block" for f in flags)
        risk = min(100, 5 + sum(RISK[f.severity] for f in flags))
        stage = "scripting" if script_stage else "rendering" if render_stage else None
        return QAOutput(
            passed=not blocked,
            risk_score=risk,
            flags=flags,
            rework_stage=stage if blocked else None,
            brand_voice_score=brand.brand_voice_score,
            prompt_version=self.prompt.ref,
            model=self.llm.model,
        )

"""Script and SEO pool. Skills: hook writer, script writer, caption (SEO) writer. No tools."""

from __future__ import annotations

import json

from pydantic import Field

from ytcore.agents.base import Agent, Reply
from ytcore.agents.context import ChannelContext
from ytcore.agents.costs import CostMeter
from ytcore.agents.models import ResearchOutput, ScriptOutput, Segment


class _Hook(Reply):
    hook: str = Field(min_length=1, max_length=400)


class _SegmentIn(Reply):
    text: str = Field(min_length=1, max_length=2000)
    fact_ids: list[str] = Field(default_factory=list)


class _Write(Reply):
    title: str = Field(min_length=1, max_length=100)
    segments: list[_SegmentIn] = Field(min_length=1, max_length=40)
    cta: str = Field(default="", max_length=400)


class _Seo(Reply):
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=60)


class ScriptSeoAgent(Agent):
    name = "script_seo"

    def run(
        self,
        research: ResearchOutput,
        ctx: ChannelContext,
        meter: CostMeter,
        *,
        revision_notes: list[str] | None = None,
    ) -> ScriptOutput:
        notes = list(revision_notes or [])
        brief = (
            "brief",
            research.brief.model_dump_json(include={"topic", "angle", "notes", "goals"}),
        )
        facts = ("research facts", json.dumps([f.model_dump() for f in research.facts]))
        guidance = ctx.guidance()
        hook = self.ask(
            "hook",
            _Hook,
            meter=meter,
            guidance=guidance,
            task="Write the opening hook.",
            data=[brief, facts, ("hook library", "\n".join(ctx.brand.hooks))],
        )
        body = self.ask(
            "write",
            _Write,
            meter=meter,
            guidance=guidance,
            task="Write the script body. Apply the revision notes if there are any.",
            data=[brief, facts, ("revision notes", "\n".join(notes))],
        )
        known = {f.id for f in research.facts}
        segments = [
            Segment(text=s.text, fact_ids=[i for i in s.fact_ids if i in known])
            for s in body.segments[: ctx.policy.max_segments]
        ]
        draft = {
            "title": body.title,
            "hook": hook.hook,
            "cta": body.cta,
            "segments": [s.model_dump() for s in segments],
        }
        seo = self.ask(
            "seo",
            _Seo,
            meter=meter,
            guidance=guidance,
            task="Write the description and tags for this script.",
            data=[("script", json.dumps(draft))],
        )
        tags: list[str] = []
        for t in seo.tags:
            t = t.strip()[:30]
            if t and t not in tags and len(tags) < 30:
                tags.append(t)
        return ScriptOutput(
            title=body.title,
            hook=hook.hook,
            segments=segments,
            cta=body.cta,
            description=seo.description,
            tags=tags,
            revision_notes=notes,
            prompt_version=self.prompt.ref,
            model=self.llm.model,
        )

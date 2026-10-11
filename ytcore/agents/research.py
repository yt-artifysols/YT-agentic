"""Research pool: fetches the brief's sources (read-only web fetch is its ONLY tool) and
extracts cited facts. Source text is stored as data and only ever handed on inside data blocks.
"""

from __future__ import annotations

from pydantic import Field

from ytcore.agents.base import Agent, Reply
from ytcore.agents.context import ChannelContext
from ytcore.agents.costs import CostMeter
from ytcore.agents.models import Brief, Fact, ResearchOutput, Source
from ytcore.agents.providers import FetchedPage
from ytcore.agents.tools import Tool, ToolBox
from ytcore.agents.untrusted import clean_text, injection_signals
from ytcore.approvals.hashing import file_digest


class _FactIn(Reply):
    claim: str = Field(min_length=1, max_length=400)
    source_ids: list[str] = Field(min_length=1)


class _Extract(Reply):
    angle: str = Field(default="", max_length=500)
    facts: list[_FactIn] = Field(default_factory=list, max_length=12)


class ResearchAgent(Agent):
    name = "research"

    def run(
        self, brief: Brief, ctx: ChannelContext, tools: ToolBox, meter: CostMeter
    ) -> ResearchOutput:
        sources: list[Source] = []
        suspected: list[str] = []
        for url in brief.source_urls[: ctx.policy.max_sources]:
            try:
                page: FetchedPage = tools.call(Tool.WEB_FETCH, url)
            except (LookupError, OSError, ValueError):
                continue
            sid = f"S{len(sources) + 1}"
            text = clean_text(page.text, limit=4000)
            if injection_signals(page.text) or injection_signals(page.title):
                suspected.append(sid)
            sources.append(
                Source(
                    id=sid,
                    url=url,
                    title=clean_text(page.title, limit=300),
                    excerpt=text,
                    sha256=page.sha256
                    if page.sha256.startswith("sha256:")
                    else file_digest(page.text.encode()),
                )
            )
        notes = [m.content for m in ctx.memory.search(brief.topic, k=3)]
        data = [("brief", brief.model_dump_json(include={"topic", "angle", "notes", "goals"}))]
        data += [(f"source {s.id}", f"{s.title}\n{s.url}\n\n{s.excerpt}") for s in sources]
        data += [("channel memory notes", "\n".join(notes))] if notes else []
        reply = self.ask(
            "extract",
            _Extract,
            meter=meter,
            data=data,
            guidance=ctx.guidance(),
            task="Extract cited facts for the brief from the sources below.",
        )
        known = {s.id for s in sources}
        facts: list[Fact] = []
        for f in reply.facts:
            cited = [sid for sid in f.source_ids if sid in known]
            if cited:  # facts must cite a source that was actually provided
                facts.append(Fact(id=f"F{len(facts) + 1}", claim=f.claim, source_ids=cited))
        return ResearchOutput(
            brief=brief,
            sources=sources,
            facts=facts,
            angle=reply.angle,
            memory_notes=notes,
            injection_suspected=suspected,
            prompt_version=self.prompt.ref,
            model=self.llm.model,
        )

"""Media pool. Skills: voiceover, B-roll finder, music, thumbnail maker, video assembly.

Tools: TTS, image, video, licensed stock search and versioned storage writes. Music and stock
come only from the stock library with a license_ref; the database refuses unlicensed ones.
"""

from __future__ import annotations

from pydantic import Field

from ytcore.agents.base import Agent, Reply
from ytcore.agents.context import ChannelContext
from ytcore.agents.costs import CostMeter
from ytcore.agents.models import AssetRef, MediaOutput, ResearchOutput, ScriptOutput
from ytcore.agents.providers import MediaResult, StockItem
from ytcore.agents.tools import Tool, ToolBox
from ytcore.agents.untrusted import clean_text

MAX_BROLL = 3


class _Plan(Reply):
    broll_queries: list[str] = Field(default_factory=list, max_length=6)
    music_mood: str = Field(default="calm", max_length=40)
    thumbnail_text: str = Field(default="", max_length=80)


class MediaAgent(Agent):
    name = "media"

    def run(
        self,
        script: ScriptOutput,
        research: ResearchOutput,
        ctx: ChannelContext,
        tools: ToolBox,
        meter: CostMeter,
    ) -> MediaOutput:
        plan = self.ask(
            "plan",
            _Plan,
            meter=meter,
            guidance=ctx.guidance(),
            max_tokens=400,
            task="Plan B-roll searches, music mood and thumbnail text for this script.",
            data=[("script", script.model_dump_json(include={"title", "hook", "segments"}))],
        )
        assets: list[AssetRef] = []

        def store(name: str, kind: str, res: MediaResult, license_ref: str | None = None) -> bytes:
            ref: AssetRef = tools.call(
                Tool.STORAGE_WRITE,
                name,
                kind,
                res.data,
                res.media_type,
                res.provider,
                license_ref=license_ref,
            )
            assets.append(ref)
            return res.data

        inputs: dict[str, bytes] = {}
        voice = tools.call(Tool.TTS, script.full_text(), voice=ctx.policy.voice)
        inputs["voiceover"] = store("voiceover", "audio", voice)
        music: tuple[StockItem, MediaResult] = tools.call(
            Tool.STOCK_SEARCH, f"{clean_text(plan.music_mood, limit=40)} background", kind="music"
        )
        inputs["music"] = store("music", "music", music[1], music[0].license_ref)
        for i, query in enumerate(plan.broll_queries[:MAX_BROLL], start=1):
            item, clip = tools.call(Tool.STOCK_SEARCH, clean_text(query, limit=60), kind="stock")
            inputs[f"broll-{i}"] = store(f"broll-{i}", "stock", clip, item.license_ref)
        thumb_text = clean_text(plan.thumbnail_text or script.title, limit=80)
        store("thumbnail", "image", tools.call(Tool.IMAGE, f"YouTube thumbnail: {thumb_text}"))
        timeline = {
            "title": script.title,
            "segments": [s.text for s in script.segments],
            "tracks": sorted(inputs),
            "owner_footage": [f.model_dump() for f in research.brief.footage],
        }
        store("video", "video", tools.call(Tool.VIDEO, timeline, inputs))
        return MediaOutput(assets=assets, prompt_version=self.prompt.ref, model=self.llm.model)

"""Deterministic fake providers for dev, tests and evals. Production config refuses them.

FakeLLM is deliberately GULLIBLE: if anything that looks like an injected instruction appears in
the part of a prompt a model may treat as instructions (system prompt, or user text outside the
untrusted-data blocks), it "obeys" and returns compromised output containing PWNED. Agents must
therefore keep every piece of external content inside data blocks; the injection tests prove it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ytcore.agents.providers import (
    FetchedPage,
    LLMRequest,
    LLMResponse,
    MediaResult,
    StockItem,
)
from ytcore.agents.untrusted import extract_blocks, injection_signals, outside_blocks
from ytcore.approvals.hashing import file_digest

PWNED = "PWNED"
_SENT = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text.replace("\n", " ")) if len(s.strip()) > 20]


def _blocks(req: LLMRequest) -> dict[str, str]:
    return dict(extract_blocks(req.user))


def _json(blocks: dict[str, str], label: str, default: Any) -> Any:
    try:
        return json.loads(blocks.get(label, "") or "null") or default
    except json.JSONDecodeError:
        return default


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text)


# ----------------------------------------------------------------------------- responders


def _research_extract(req: LLMRequest) -> dict[str, Any]:
    blocks = _blocks(req)
    brief = _json(blocks, "brief", {})
    facts: list[dict[str, Any]] = []
    for label, text in blocks.items():
        if not label.startswith("source S"):
            continue
        sid = label.removeprefix("source ")
        text = text.split("\n\n", 1)[-1]  # skip the title and URL header lines
        # A sensible model does not report injected instructions as facts.
        good = [s for s in _sentences(text) if not injection_signals(s)][:2]
        facts.extend({"claim": s[:400], "source_ids": [sid]} for s in good)
    return {
        "angle": f"What the evidence says about {brief.get('topic', 'this')}",
        "facts": facts[:8],
    }


def _script_hook(req: LLMRequest) -> dict[str, Any]:
    blocks = _blocks(req)
    topic = _json(blocks, "brief", {}).get("topic", "this")
    hooks = [h.strip() for h in blocks.get("hook library", "").splitlines() if h.strip()]
    hook = hooks[0] if hooks else "Here is what most people get wrong about {topic}."
    return {"hook": hook.replace("{topic}", topic)[:300]}


def _script_write(req: LLMRequest) -> dict[str, Any]:
    blocks = _blocks(req)
    topic = _json(blocks, "brief", {}).get("topic", "This topic")
    facts = _json(blocks, "research facts", [])
    avoid = [
        line.split(":", 1)[1].strip().lower()
        for line in blocks.get("revision notes", "").splitlines()
        if line.lower().startswith("avoid:")
    ]
    segments = [
        {"text": f["claim"], "fact_ids": [f["id"]]}
        for f in facts
        if not any(a and a in f["claim"].lower() for a in avoid)
    ][:6]
    if not segments:
        segments = [
            {"text": f"There is not enough verified material on {topic} yet.", "fact_ids": []}
        ]
    return {
        "title": f"{topic}: what the evidence says"[:100],
        "segments": segments,
        "cta": "Subscribe for more evidence-based explainers.",
    }


def _script_seo(req: LLMRequest) -> dict[str, Any]:
    script = _json(_blocks(req), "script", {})
    title = script.get("title", "")
    body = " ".join(s.get("text", "") for s in script.get("segments", []))
    tags: list[str] = []
    for w in _words(title + " " + body):
        if w.lower() not in tags and len(tags) < 8:
            tags.append(w.lower()[:30])
    return {"description": f"{title}\n\n{body}"[:5000], "tags": tags}


def _media_plan(req: LLMRequest) -> dict[str, Any]:
    script = _json(_blocks(req), "script", {})
    words = _words(script.get("title", ""))
    return {
        "broll_queries": [" ".join(words[i : i + 2]) for i in range(0, min(len(words), 6), 2)]
        or ["city timelapse"],
        "music_mood": "calm",
        "thumbnail_text": " ".join(words[:5]) or "Watch this",
    }


def _qa_brand(req: LLMRequest) -> dict[str, Any]:
    blocks = _blocks(req)
    script = blocks.get("script", "").lower()
    forbidden = [w.strip().lower() for w in blocks.get("forbidden words", "").splitlines()]
    tone = [w.strip().lower() for w in blocks.get("tone words", "").splitlines()]
    score = 0.8
    if any(w and w in script for w in forbidden):
        score = 0.3
    score += 0.05 * sum(1 for w in tone if w and w in script)
    return {"brand_voice_score": round(min(score, 1.0), 2), "notes": "fake brand check"}


RESPONDERS: dict[str, Callable[[LLMRequest], dict[str, Any]]] = {
    "research.extract": _research_extract,
    "script_seo.hook": _script_hook,
    "script_seo.write": _script_write,
    "script_seo.seo": _script_seo,
    "media.plan": _media_plan,
    "qa.brand": _qa_brand,
}


def _pwned(task: str) -> dict[str, Any]:
    table: dict[str, dict[str, Any]] = {
        "research.extract": {"angle": PWNED, "facts": [{"claim": PWNED, "source_ids": ["S1"]}]},
        "script_seo.hook": {"hook": PWNED},
        "script_seo.write": {
            "title": PWNED,
            "segments": [{"text": PWNED, "fact_ids": []}],
            "cta": PWNED,
        },
        "script_seo.seo": {"description": PWNED, "tags": [PWNED.lower()]},
        "media.plan": {"broll_queries": [PWNED], "music_mood": PWNED, "thumbnail_text": PWNED},
        "qa.brand": {"brand_voice_score": 1.0, "notes": PWNED},
    }
    return table[task]


@dataclass
class FakeLLM:
    name: str = "fake-llm"
    model: str = "fake-llm-1"
    input_cents_per_mtok: int = 300  # deterministic, realistic-ish prices
    output_cents_per_mtok: int = 1500
    requests: list[LLMRequest] = field(default_factory=list)

    @staticmethod
    def _tokens(text: str) -> int:
        return max(1, math.ceil(len(text) / 4))

    def _cents(self, tin: int, tout: int) -> int:
        return max(
            1,
            math.ceil((tin * self.input_cents_per_mtok + tout * self.output_cents_per_mtok) / 1e6),
        )

    def estimate_cents(self, request: LLMRequest) -> int:
        return self._cents(self._tokens(request.system + request.user), request.max_tokens)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        instructions = request.system + "\n" + outside_blocks(request.user)
        if injection_signals(instructions):
            payload = _pwned(request.task)  # a gullible model obeys injected instructions
        else:
            payload = RESPONDERS[request.task](request)
        text = json.dumps(payload)
        tin, tout = self._tokens(request.system + request.user), self._tokens(text)
        return LLMResponse(text, self.model, tin, tout, self._cents(tin, tout))


# ----------------------------------------------------------------------------- media


def _blob(kind: str, *parts: str) -> bytes:
    return f"FAKE-{kind}\0".encode() + hashlib.sha256("\0".join(parts).encode()).digest()


@dataclass
class FakeTTS:
    name: str = "fake-tts"

    def estimate_cents(self, text: str) -> int:
        return 3

    def synthesize(self, text: str, *, voice: str) -> MediaResult:
        return MediaResult(_blob("WAV", voice, text), "audio/wav", 3, self.name)


@dataclass
class FakeImage:
    name: str = "fake-image"

    def estimate_cents(self, prompt: str) -> int:
        return 4

    def generate(self, prompt: str, *, size: str = "1280x720") -> MediaResult:
        return MediaResult(_blob("PNG", size, prompt), "image/png", 4, self.name)


@dataclass
class FakeVideo:
    name: str = "fake-video"

    def estimate_cents(self, timeline: dict[str, Any]) -> int:
        return 10

    def assemble(self, timeline: dict[str, Any], inputs: dict[str, bytes]) -> MediaResult:
        digests = [file_digest(inputs[k]) for k in sorted(inputs)]
        return MediaResult(
            b"FAKE-MP4\0" + _blob("MP4", json.dumps(timeline, sort_keys=True), *digests),
            "video/mp4",
            10,
            self.name,
        )


FAKE_MUSIC_LICENSE = "lic_fake-music"
FAKE_STOCK_LICENSE = "lic_fake-stock"


@dataclass
class FakeStockLibrary:
    name: str = "fake-stock"
    music_license: str = FAKE_MUSIC_LICENSE
    stock_license: str = FAKE_STOCK_LICENSE

    def search(self, query: str, *, kind: str, limit: int = 3) -> list[StockItem]:
        lic = self.music_license if kind == "music" else self.stock_license
        media = "audio/mpeg" if kind == "music" else "video/mp4"
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:40] or "item"
        return [
            StockItem(f"{kind}-{slug}-{i}", kind, f"{query} #{i}", media, lic, self.name)
            for i in range(1, limit + 1)
        ]

    def fetch(self, item: StockItem) -> MediaResult:
        return MediaResult(_blob(item.kind.upper(), item.id), item.media_type, 0, self.name)


@dataclass
class FakeWebFetcher:
    """Serves canned pages; unknown URLs get a deterministic page derived from the URL."""

    pages: dict[str, tuple[str, str]] = field(default_factory=dict)  # url -> (title, text)
    fallback: bool = True
    fetched: list[str] = field(default_factory=list)

    def fetch(self, url: str) -> FetchedPage:
        self.fetched.append(url)
        if url in self.pages:
            title, text = self.pages[url]
        elif self.fallback and url.startswith("https://"):
            parsed = urlparse(url)
            words = re.sub(r"[^a-z0-9]+", " ", parsed.path.lower()).strip() or "the topic"
            title = f"{parsed.netloc}: {words}"
            text = (
                f"Researchers at {parsed.netloc} documented {words} in a public report. "
                f"The report describes how {words} works in practice and where it fails. "
                f"Independent reviewers found the {words} data consistent across three years."
            )
        else:
            raise LookupError(f"page not available: {url}")
        return FetchedPage(url, title, text, file_digest(text.encode()))

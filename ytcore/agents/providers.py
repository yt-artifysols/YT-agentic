"""Provider adapter interfaces (LLM, TTS, image, video, stock media, web fetch).

Agents depend only on these protocols. Fakes live in fake.py; real adapters are selected by
config (ytcore/agents/factory.py) and get credentials only through the SecretsProvider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMRequest:
    task: str  # "<agent>.<skill>", for routing and cost attribution
    system: str  # trusted instructions only
    user: str  # task text + untrusted data blocks
    max_tokens: int = 1500
    temperature: float = 0.0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_cents: int


class LLMProvider(Protocol):
    name: str
    model: str

    def estimate_cents(self, request: LLMRequest) -> int:
        """Upper bound for the call's cost (used to refuse calls that would break a budget)."""
        ...

    def complete(self, request: LLMRequest) -> LLMResponse: ...


@dataclass(frozen=True)
class MediaResult:
    data: bytes
    media_type: str
    cost_cents: int
    provider: str
    meta: dict[str, Any] = field(default_factory=dict)


class TTSProvider(Protocol):
    name: str

    def estimate_cents(self, text: str) -> int: ...

    def synthesize(self, text: str, *, voice: str) -> MediaResult: ...


class ImageProvider(Protocol):
    name: str

    def estimate_cents(self, prompt: str) -> int: ...

    def generate(self, prompt: str, *, size: str = "1280x720") -> MediaResult: ...


class VideoProvider(Protocol):
    name: str

    def estimate_cents(self, timeline: dict[str, Any]) -> int: ...

    def assemble(self, timeline: dict[str, Any], inputs: dict[str, bytes]) -> MediaResult: ...


@dataclass(frozen=True)
class StockItem:
    id: str
    kind: str  # music | stock
    title: str
    media_type: str
    license_ref: str
    provider: str


class StockLibrary(Protocol):
    name: str

    def search(self, query: str, *, kind: str, limit: int = 3) -> list[StockItem]: ...

    def fetch(self, item: StockItem) -> MediaResult: ...


@dataclass(frozen=True)
class FetchedPage:
    url: str
    title: str
    text: str
    sha256: str


class WebFetcher(Protocol):
    """Read-only: GET requests only, no cookies, no form posts."""

    def fetch(self, url: str) -> FetchedPage: ...

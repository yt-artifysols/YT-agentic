"""Explicit tool allowlist per agent pool, enforced in code.

Tools are called by agent code, never because a model asked for them: model output is parsed
into strict schemas and has no way to name a tool. A ToolBox only binds the tools its agent is
allowed; anything else raises ToolNotAllowed. Every call is logged for tests and audits.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Tool(StrEnum):
    WEB_FETCH = "web_fetch"  # read-only HTTP GET of research sources
    TTS = "tts"
    IMAGE = "image"
    VIDEO = "video"
    STOCK_SEARCH = "stock_search"  # licensed music / stock footage
    STORAGE_WRITE = "storage_write"  # versioned, content-addressed assets under the channel
    STORAGE_READ = "storage_read"
    RIGHTS_READ = "rights_read"  # rights and license register


ALLOWLIST: Mapping[str, frozenset[Tool]] = {
    "research": frozenset({Tool.WEB_FETCH}),
    "script_seo": frozenset(),
    "media": frozenset({Tool.TTS, Tool.IMAGE, Tool.VIDEO, Tool.STOCK_SEARCH, Tool.STORAGE_WRITE}),
    "qa": frozenset({Tool.STORAGE_READ, Tool.RIGHTS_READ}),
}


class ToolNotAllowed(PermissionError):
    pass


@dataclass(frozen=True)
class ToolCall:
    agent: str
    tool: Tool
    summary: str


@dataclass
class ToolBox:
    agent: str
    _impls: dict[Tool, Callable[..., Any]] = field(default_factory=dict)
    calls: list[ToolCall] = field(default_factory=list)

    @classmethod
    def for_agent(cls, agent: str, available: Mapping[Tool, Callable[..., Any]]) -> ToolBox:
        allowed = ALLOWLIST.get(agent)
        if allowed is None:
            raise ToolNotAllowed(f"unknown agent {agent!r}")
        return cls(agent, {t: fn for t, fn in available.items() if t in allowed})

    @property
    def allowed(self) -> frozenset[Tool]:
        return ALLOWLIST[self.agent]

    def call(self, tool: Tool, *args: Any, **kwargs: Any) -> Any:
        if tool not in self.allowed:
            raise ToolNotAllowed(f"agent {self.agent!r} may not use tool {tool.value!r}")
        fn = self._impls.get(tool)
        if fn is None:
            raise ToolNotAllowed(f"tool {tool.value!r} is not configured")
        summary = ", ".join(str(a)[:80] for a in args if isinstance(a, str | int))
        self.calls.append(ToolCall(self.agent, tool, summary))
        return fn(*args, **kwargs)

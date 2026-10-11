"""Shared agent plumbing: prompt assembly with data blocks, metered LLM calls, strict parsing."""

from __future__ import annotations

import json
from typing import ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from ytcore.agents.costs import CostMeter
from ytcore.agents.prompts import Prompt, load_prompt
from ytcore.agents.providers import LLMProvider, LLMRequest
from ytcore.agents.untrusted import data_block

T = TypeVar("T", bound=BaseModel)


class AgentError(Exception):
    """An agent could not produce valid output."""


class Reply(BaseModel):
    """Base for model replies: unknown fields (e.g. smuggled tool calls) are rejected."""

    model_config = ConfigDict(extra="forbid")


def parse_reply[R: BaseModel](text: str, schema: type[R]) -> R:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise AgentError("model reply contains no JSON object")
    try:
        return schema.model_validate(json.loads(text[start : end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise AgentError(f"model reply does not match {schema.__name__}: {exc}") from None


class Agent:
    name: ClassVar[str]

    def __init__(self, llm: LLMProvider, *, prompt_version: str | None = None) -> None:
        self.llm = llm
        self.prompt: Prompt = load_prompt(self.name, prompt_version)

    def ask(
        self,
        skill: str,
        schema: type[T],
        *,
        meter: CostMeter,
        task: str,
        data: list[tuple[str, str]],
        guidance: str = "",
        max_tokens: int = 1500,
    ) -> T:
        """`task` is fixed trusted text from code; everything variable goes into `data`."""
        user = "\n\n".join([task, *(data_block(label, content) for label, content in data)])
        req = LLMRequest(
            task=f"{self.name}.{skill}",
            system=self.prompt.system_for(skill, guidance),
            user=user,
            max_tokens=max_tokens,
        )
        meter.authorize(self.llm.estimate_cents(req), req.task)
        resp = self.llm.complete(req)
        meter.charge(
            "llm",
            resp.cost_cents,
            provider=self.llm.name,
            what=req.task,
            model=resp.model,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            prompt=self.prompt.ref,
        )
        return parse_reply(resp.text, schema)

"""Real LLM adapter: Claude through the official Anthropic SDK (`agents` extra).

The API key is read through the SecretsProvider on first use and never logged. Requests carry
no tools, so a model cannot call anything; replies are parsed into strict schemas by the
agents. Refusal fallbacks are enabled server-side (`fallbacks: "default"`).
"""

from __future__ import annotations

import math
from typing import Any

from ytcore.agents.base import AgentError
from ytcore.agents.providers import LLMRequest, LLMResponse
from ytcore.secrets.provider import SecretRef, SecretsProvider

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMRefused(AgentError):
    """The model declined the request (stop_reason "refusal") on every fallback."""


class AnthropicLLM:
    name = "anthropic"

    def __init__(
        self,
        secrets: SecretsProvider,
        *,
        api_key_ref: str,
        model: str = "claude-opus-5-5",
        effort: str = "medium",
        input_cents_per_mtok: int = 400,
        output_cents_per_mtok: int = 2000,
        client: Any = None,
    ) -> None:
        self._secrets, self._key_ref = secrets, SecretRef(api_key_ref)
        self.model, self._effort = model, effort
        self._in, self._out = input_cents_per_mtok, output_cents_per_mtok
        self._client = client

    def _api(self) -> Any:
        if self._client is None:
            import anthropic  # optional dependency: pip install '.[agents]'

            key = self._secrets.get(self._key_ref)
            self._client = anthropic.Anthropic(api_key=key.reveal())
        return self._client

    def _cents(self, tin: int, tout: int) -> int:
        return max(1, math.ceil((tin * self._in + tout * self._out) / 1e6))

    def estimate_cents(self, request: LLMRequest) -> int:
        # Upper bound: a conservative 3 chars per input token plus the full output allowance
        # (thinking tokens are billed as output).
        tin = math.ceil(len(request.system + request.user) / 3)
        return self._cents(tin, request.max_tokens)

    def complete(self, request: LLMRequest) -> LLMResponse:
        resp = self._api().beta.messages.create(
            model=self.model,
            max_tokens=request.max_tokens,
            system=request.system,
            messages=[{"role": "user", "content": request.user}],
            output_config={"effort": self._effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            raise LLMRefused(f"{request.task}: model declined the request")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        tin, tout = int(resp.usage.input_tokens), int(resp.usage.output_tokens)
        return LLMResponse(text, str(resp.model), tin, tout, self._cents(tin, tout))

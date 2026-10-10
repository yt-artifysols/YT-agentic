"""Real provider adapters without network: Claude adapter request shape, refusals and secrets;
web fetcher refuses non-https and non-public targets."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ytcore.agents.anthropic_llm import FALLBACK_BETA, AnthropicLLM, LLMRefused
from ytcore.agents.factory import build_llm, build_web
from ytcore.agents.fake import FakeLLM, FakeWebFetcher
from ytcore.agents.providers import LLMRequest
from ytcore.agents.web import HttpWebFetcher
from ytcore.config.settings import Settings
from ytcore.secrets.provider import InMemorySecretsProvider


class _Messages:
    def __init__(self, stop: str = "end_turn") -> None:
        self.calls: list[dict[str, Any]] = []
        self.stop = stop

    def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return SimpleNamespace(
            stop_reason=self.stop,
            model=kw["model"],
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text='{"hook": "hi"}')],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
        )  # fmt: skip


def _llm(stop: str = "end_turn") -> tuple[AnthropicLLM, _Messages]:
    msgs = _Messages(stop)
    client = SimpleNamespace(beta=SimpleNamespace(messages=msgs))
    secrets = InMemorySecretsProvider({"env://ANTHROPIC_API_KEY": "test-key"})
    return AnthropicLLM(secrets, api_key_ref="env://ANTHROPIC_API_KEY", client=client), msgs


def test_claude_adapter_sends_no_tools_and_meters_cost() -> None:
    llm, msgs = _llm()
    req = LLMRequest("script_seo.hook", "system text", "user text", max_tokens=800)
    resp = llm.complete(req)
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["system"] == "system text"
    assert call["messages"] == [{"role": "user", "content": "user text"}]
    assert "tools" not in call and call["betas"] == [FALLBACK_BETA]
    assert call["fallbacks"] == "default" and call["output_config"] == {"effort": "medium"}
    assert resp.text == '{"hook": "hi"}'
    assert resp.cost_cents == 2  # 1000 in @ $4/MTok + 500 out @ $20/MTok = 1.4 cents
    assert llm.estimate_cents(req) >= resp.cost_cents


def test_claude_adapter_raises_on_refusal() -> None:
    llm, _ = _llm("refusal")
    with pytest.raises(LLMRefused):
        llm.complete(LLMRequest("qa.brand", "s", "u"))


def test_factory_defaults_to_fakes() -> None:
    settings = Settings()
    assert isinstance(build_llm(settings, InMemorySecretsProvider()), FakeLLM)
    assert isinstance(build_web(settings), FakeWebFetcher)
    assert isinstance(build_llm(Settings(llm_backend="anthropic"), InMemorySecretsProvider()),
                      AnthropicLLM)  # fmt: skip


@pytest.mark.parametrize(
    "url", ["http://example.com/", "file:///etc/passwd", "https://127.0.0.1/", "https://localhost/",
            "https://169.254.169.254/latest/meta-data", "https://10.0.0.5/"],
)  # fmt: skip
def test_web_fetcher_refuses_non_public_or_non_https(url: str) -> None:
    with pytest.raises(ValueError):
        HttpWebFetcher().fetch(url)

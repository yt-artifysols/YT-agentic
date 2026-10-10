"""Build providers and the agent pipeline from settings. Fakes by default; production refuses
them (ytcore/config/settings.py)."""

from __future__ import annotations

from ytcore.agents.fake import (
    FakeImage,
    FakeLLM,
    FakeStockLibrary,
    FakeTTS,
    FakeVideo,
    FakeWebFetcher,
)
from ytcore.agents.memory import PostgresMemory
from ytcore.agents.pipeline import AgentPipeline, Providers
from ytcore.agents.providers import LLMProvider, WebFetcher
from ytcore.agents.store import AssetStore
from ytcore.config.settings import Settings
from ytcore.inputs.store import PostgresInputs
from ytcore.knowledge.store import PostgresKnowledge
from ytcore.secrets.provider import SecretsProvider
from ytcore.store.production import PostgresProductionStore


def build_llm(settings: Settings, secrets: SecretsProvider) -> LLMProvider:
    if settings.llm_backend == "anthropic":
        from ytcore.agents.anthropic_llm import AnthropicLLM

        return AnthropicLLM(
            secrets,
            api_key_ref=settings.llm_api_key_ref,
            model=settings.llm_model,
            effort=settings.llm_effort,
            input_cents_per_mtok=settings.llm_input_cents_per_mtok,
            output_cents_per_mtok=settings.llm_output_cents_per_mtok,
        )
    return FakeLLM()


def build_web(settings: Settings) -> WebFetcher:
    if settings.web_backend == "http":
        from ytcore.agents.web import HttpWebFetcher

        return HttpWebFetcher()
    return FakeWebFetcher()


def build_providers(settings: Settings, secrets: SecretsProvider) -> Providers:
    return Providers(
        llm=build_llm(settings, secrets),
        tts=FakeTTS(),
        image=FakeImage(),
        video=FakeVideo(),
        stock=FakeStockLibrary(),
        web=build_web(settings),
    )


def build_postgres_pipeline(
    conninfo: str, assets: AssetStore, providers: Providers
) -> AgentPipeline:
    import psycopg

    def connect() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(conninfo, autocommit=True)

    return AgentPipeline(
        store=PostgresProductionStore(connect),
        knowledge=PostgresKnowledge(connect),
        inputs=PostgresInputs(connect),
        memory=PostgresMemory(connect),
        assets=assets,
        providers=providers,
    )

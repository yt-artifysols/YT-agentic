"""Eval harness: committed baselines pass, regressions and unrecorded prompt changes fail."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from ytcore.agents.fake import FakeLLM
from ytcore.agents.providers import LLMRequest, LLMResponse
from ytcore.evals import __main__ as cli
from ytcore.evals.harness import AGENTS, evaluate, load_samples


def test_there_are_at_least_twenty_sample_briefs() -> None:
    samples = load_samples()
    assert len(samples) >= 20
    assert any("untrusted_instructions_ignored" in s.expect_flags for s in samples)
    assert any("banned_claim" in s.expect_flags for s in samples)


def test_committed_baselines_pass(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run", "--agent", "all"], llm=FakeLLM()) == 0
    out = capsys.readouterr().out
    assert all(f"ok   {agent}" in out for agent in AGENTS)


@dataclass
class SloppyLLM(FakeLLM):
    """A 'new model' that drops citations and repeats claims it should not make."""

    model: str = "fake-llm-1"

    def complete(self, request: LLMRequest) -> LLMResponse:
        resp = super().complete(request)
        if request.task != "script_seo.write":
            return resp
        body = json.loads(resp.text)
        for seg in body["segments"]:
            seg["fact_ids"] = []
        body["segments"].append({"text": "This is risk-free profit for everyone.", "fact_ids": []})
        return LLMResponse(json.dumps(body), resp.model, resp.input_tokens, resp.output_tokens,
                           resp.cost_cents)  # fmt: skip


def test_a_worse_model_fails_against_the_baseline(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run", "--agent", "script"], llm=SloppyLLM()) == 1
    assert "policy_compliance" in capsys.readouterr().out


def test_update_baseline_refuses_a_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for f in cli.BASELINES.glob("script.*.json"):
        (tmp_path / f.name).write_text(f.read_text())
    monkeypatch.setattr(cli, "BASELINES", tmp_path)
    before = (tmp_path / "script.fake-llm-1.json").read_text()
    assert cli.main(["run", "--agent", "script", "--update-baseline"], llm=SloppyLLM()) == 1
    assert (tmp_path / "script.fake-llm-1.json").read_text() == before


def test_prompt_change_without_a_new_baseline_fails() -> None:
    result = evaluate("research", FakeLLM())
    baseline = json.loads(cli.baseline_path("research", "fake-llm-1").read_text())
    assert cli.compare(result, baseline) == []
    changed = {**result, "prompts": {"research": "research.v2@000000000000"}}
    assert any("prompt files changed" in p for p in cli.compare(changed, baseline))

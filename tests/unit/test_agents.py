"""Agent pools v1: end-to-end pipeline, provenance, tools, isolation, budgets and knowledge rules
(in memory, fake providers)."""

from __future__ import annotations

import json

import pytest

from tests.agentkit import SOURCE_URL, Kit
from ytcore.agents.base import AgentError, parse_reply
from ytcore.agents.models import ResearchOutput, ScriptOutput
from ytcore.agents.script_seo import _Hook
from ytcore.agents.tools import ALLOWLIST, Tool, ToolBox, ToolNotAllowed
from ytcore.approvals.hashing import content_hash
from ytcore.gateway.assets import read_verified
from ytcore.knowledge.models import KnowledgeKind
from ytcore.store.errors import (
    BudgetExceeded,
    JobBudgetExceeded,
    KnowledgeForbidden,
    KnowledgeStale,
    StoreError,
)


def _doc(kit: Kit, chan: str, digest: str, schema: type) -> object:
    return schema.model_validate_json(read_verified(kit.assets, f"channels/{chan}/", digest))


# ----------------------------------------------------------------------------- pipeline


def test_pipeline_produces_a_sealed_package_with_provenance() -> None:
    kit = Kit()
    chan = kit.channel()
    idea = kit.idea(chan, "Why index funds beat stock picking")
    goal = kit.inputs.add(chan, "goal", body="audience: new investors", submitted_by="owner")  # type: ignore[arg-type]
    job = kit.job(chan)
    res = kit.produce(job, chan)

    assert res.qa.passed and res.reworks == 0
    meta, prov = res.manifest["metadata"], res.manifest["provenance"]
    assert meta["title"].startswith("Why index funds beat stock picking")
    assert meta["contains_synthetic_media"] is True and meta["privacy_status"] == "private"
    assert {
        "video",
        "thumbnail",
        "voiceover",
        "music",
        "research.json",
        "script.json",
        "media.json",
    } <= set(res.manifest["assets"])
    assert prov["owner_inputs"] == [idea] and prov["goals"] == [goal.id]
    assert prov["knowledge"] == {
        "banned_claims": 1,
        "brand_voice": 1,
        "hook_library": 1,
        "policy": 1,
    }
    assert prov["sources"][0]["url"] == SOURCE_URL
    assert set(prov["prompts"]) == {"research", "script_seo", "media"}
    # Music and stock carry licenses; every asset is stored under the channel prefix.
    info = res.manifest["asset_info"]
    assert info["music"]["license_ref"] and info["broll-1"]["license_ref"]
    for digest in res.manifest["assets"].values():
        read_verified(kit.assets, f"channels/{chan}/", digest)
    # The idea is consumed by this job; the goal stays available.
    assert kit.inputs.inputs[idea].status == "used"
    assert kit.inputs.inputs[idea].used_by_job == job
    assert kit.inputs.inputs[goal.id].status == "new"
    # Knowledge versions used are recorded for the job.
    assert kit.knowledge.channel(chan).pinned(job).versions() == prov["knowledge"]
    # Costs were metered per call and stay within the job budget.
    assert 0 < sum(c[3] for c in kit.store.costs) <= 1000


def test_new_owner_inputs_are_consumed_first_and_drive_the_content_hash() -> None:
    kit = Kit()
    chan = kit.channel()
    first = kit.idea(chan, "First idea about index funds")
    second = kit.idea(chan, "Second idea about index funds")
    a = kit.produce(kit.job(chan), chan)
    b = kit.produce(kit.job(chan), chan)
    assert a.manifest["provenance"]["owner_inputs"] == [first]
    assert b.manifest["provenance"]["owner_inputs"] == [second]
    # Same content, different provenance -> different hash (the approval binds provenance).
    same_meta = {
        **b.manifest,
        "metadata": a.manifest["metadata"],
        "assets": a.manifest["assets"],
        "asset_info": a.manifest["asset_info"],
    }
    assert content_hash(same_meta) != a.content_hash


def test_without_inputs_the_playbook_drives_the_brief() -> None:
    kit = Kit()
    chan = kit.channel()
    kit.knowledge.seed_approved(chan, KnowledgeKind.PLAYBOOK, "# topics\n- Emergency funds 101")
    res = kit.produce(kit.job(chan), chan)
    research = _doc(kit, chan, res.artifacts["research.json"], ResearchOutput)
    assert isinstance(research, ResearchOutput) and research.brief.topic == "Emergency funds 101"
    assert res.manifest["provenance"]["owner_inputs"] == []


def test_banned_claim_triggers_one_script_rework() -> None:
    kit = Kit()
    chan = kit.channel()
    kit.web.pages[SOURCE_URL] = (
        "Miracle fund",
        "This fund offers guaranteed returns to every investor. "
        "Index funds track a market index and charge low fees.",
    )
    kit.idea(chan, "Is this fund any good")
    res = kit.produce(kit.job(chan), chan)
    assert res.qa.passed and res.reworks == 1
    script = _doc(kit, chan, res.artifacts["script.json"], ScriptOutput)
    assert isinstance(script, ScriptOutput)
    assert "guaranteed returns" not in script.full_text().lower()
    assert script.revision_notes == ["avoid: guaranteed returns"]


# ----------------------------------------------------------------------------- tools


def test_tool_allowlist_is_explicit() -> None:
    assert {
        "research": {Tool.WEB_FETCH},
        "script_seo": set(),
        "media": {Tool.TTS, Tool.IMAGE, Tool.VIDEO, Tool.STOCK_SEARCH, Tool.STORAGE_WRITE},
        "qa": {Tool.STORAGE_READ, Tool.RIGHTS_READ},
    } == ALLOWLIST
    everything = {t: (lambda *a, **k: None) for t in Tool}
    script = ToolBox.for_agent("script_seo", everything)
    for tool in Tool:
        with pytest.raises(ToolNotAllowed):
            script.call(tool)
    with pytest.raises(ToolNotAllowed):
        ToolBox.for_agent("research", everything).call(Tool.STORAGE_WRITE, "x")
    with pytest.raises(ToolNotAllowed):
        ToolBox.for_agent("qa", everything).call(Tool.TTS, "x")
    with pytest.raises(ToolNotAllowed):
        ToolBox.for_agent("publisher", everything)


def test_pipeline_agents_only_use_their_tools() -> None:
    kit = Kit()
    chan = kit.channel()
    kit.idea(chan, "Index funds")
    kit.produce(kit.job(chan), chan)
    used = {box.agent: {c.tool for c in box.calls} for box in kit.p.toolboxes}
    assert used["research"] == {Tool.WEB_FETCH}
    assert used["media"] <= ALLOWLIST["media"]
    assert used["qa"] == {Tool.STORAGE_READ, Tool.RIGHTS_READ}
    assert "script_seo" not in used  # the script pool gets no toolbox at all


def test_model_replies_cannot_smuggle_tool_calls() -> None:
    with pytest.raises(AgentError):
        parse_reply('{"hook": "x", "tool_call": {"name": "publish"}}', _Hook)
    with pytest.raises(AgentError):
        parse_reply("I will now publish the video.", _Hook)


# ----------------------------------------------------------------------------- isolation


def test_channel_memory_and_knowledge_are_isolated() -> None:
    kit = Kit()
    a, b = kit.channel("chan-a"), kit.channel("chan-b", seed=False)
    kit.memory.for_channel(a).add("fact", "secret plan for channel A index funds")
    assert kit.memory.for_channel(b).search("secret plan index funds") == []
    assert kit.memory.for_channel(a).search("secret plan")[0].content.startswith("secret plan")
    assert kit.knowledge.channel(b).latest_approved().versions() == {}
    with pytest.raises(StoreError):
        kit.knowledge.channel(b).pin("job-x", kit.knowledge.channel(a).latest_approved(), actor="t")
    # A job of channel B never sees channel A's knowledge or memory.
    kit.idea(b, "Index funds for B")
    job = kit.job(b)
    res = kit.produce(job, b)
    assert res.manifest["provenance"]["knowledge"] == {}
    research = _doc(kit, b, res.artifacts["research.json"], ResearchOutput)
    assert isinstance(research, ResearchOutput) and research.memory_notes == []
    assert all("secret plan" not in r.user for r in kit.llm.requests)


def test_assets_of_another_channel_cannot_be_recorded_for_a_job() -> None:
    kit = Kit()
    kit.channel("chan-a")
    kit.channel("chan-b", seed=False)
    job = kit.job("chan-a")
    with pytest.raises(StoreError):
        kit.store.record_asset(
            job,
            "chan-b",
            name="video",
            kind="video",
            sha256="sha256:" + "0" * 64,
            media_type="video/mp4",
            provider="x",
            license_ref=None,
        )


# ----------------------------------------------------------------------------- budgets


def test_job_budget_cap_fails_before_the_call_and_alerts() -> None:
    kit = Kit()
    chan = kit.channel(job_budget_cents=1)
    kit.idea(chan, "Index funds")
    job = kit.job(chan)
    with pytest.raises(JobBudgetExceeded):
        kit.p.run_stage(job, chan, "research", {})
    assert kit.llm.requests == []  # refused before spending anything
    assert kit.store.alerts and kit.store.alerts[0][2] == "budget.job"
    assert kit.store.costs == []


def test_channel_daily_budget_cap_stops_later_jobs() -> None:
    kit = Kit()
    chan = kit.channel(daily_budget_cents=40)
    kit.idea(chan, "Index funds one")
    kit.idea(chan, "Index funds two")
    kit.produce(kit.job(chan), chan)
    with pytest.raises(BudgetExceeded):
        kit.produce(kit.job(chan), chan)
    assert any(a[2] == "budget.channel" for a in kit.store.alerts)
    assert sum(c[3] for c in kit.store.costs) <= 40


# ----------------------------------------------------------------------------- knowledge


def test_agents_can_only_submit_knowledge_drafts() -> None:
    kit = Kit()
    chan = kit.channel()
    kb = kit.knowledge.channel(chan)
    assert not hasattr(kb, "approve") and not hasattr(kb, "decide")
    draft = kb.propose(
        KnowledgeKind.BANNED_CLAIMS,
        "guaranteed returns\nrisk-free",
        proposed_by="agent:qa",
        base_version=1,
    )
    assert draft.status == "draft"
    assert kb.latest_approved().banned_claims == ["guaranteed returns", "cures any disease"]
    with pytest.raises(KnowledgeForbidden):
        kit.knowledge.decide(draft.id, approver="agent:qa", approve=True)
    kit.knowledge.human_approvers.add("alice")
    kit.knowledge.decide(draft.id, approver="alice", approve=True)
    assert kb.latest_approved().banned_claims == ["guaranteed returns", "risk-free"]
    stale = kb.propose(KnowledgeKind.BANNED_CLAIMS, "x", proposed_by="agent:qa", base_version=1)
    with pytest.raises(KnowledgeStale):
        kit.knowledge.decide(stale.id, approver="alice", approve=True)


def test_jobs_keep_the_knowledge_they_started_with() -> None:
    kit = Kit()
    chan = kit.channel()
    kit.idea(chan, "Index funds")
    job = kit.job(chan)
    artifacts = kit.p.run_stage(job, chan, "research", {})
    kit.knowledge.seed_approved(chan, KnowledgeKind.BRAND_VOICE, "Loud and punchy.")
    artifacts |= kit.p.run_stage(job, chan, "script", artifacts)
    artifacts |= kit.p.run_stage(job, chan, "render", artifacts)
    manifest = kit.p.build_manifest(job, chan, artifacts)
    assert manifest["provenance"]["knowledge"]["brand_voice"] == 1
    script_requests = [r for r in kit.llm.requests if r.task.startswith("script_seo")]
    assert all("Loud and punchy" not in r.system for r in script_requests)
    assert json.dumps(manifest).count("Loud and punchy") == 0

"""Safety in the agent pipeline: risk stored on the job, per-channel policy handling, policy
rules, cross-channel duplicates (and that no duplicate detail ever reaches a model), provenance,
asset limits and the disclosure gate."""

from __future__ import annotations

import pytest

from tests.agentkit import Kit
from ytcore.agents.fake import FakeImage
from ytcore.agents.manifest import build_manifest
from ytcore.agents.providers import MediaResult
from ytcore.safety.assets import AssetRejected
from ytcore.safety.checks import ensure_disclosed

SOURCE_B = (
    "Index funds track a market index and charge low fees. "
    "Over twenty years most active funds trailed their benchmark index. "
    "Diversification lowers the risk of a single company failing."
)


def _flags(res) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {f["code"]: f["severity"] for f in res.qa.flags}


def _run(kit: Kit, chan: str, idea: str = "Why index funds beat stock picking", **kw):  # type: ignore[no-untyped-def]
    kit.idea(chan, idea, **{k: v for k, v in kw.items() if k == "url"})
    return kit.produce(kit.job(chan), chan, max_rework=kw.get("max_rework", 0))


def test_risk_is_stored_on_the_job_with_named_reasons() -> None:
    kit = Kit()
    chan = kit.channel(safety={"sensitive": {"finance": "escalate"}})
    res = _run(kit, chan)
    _job, channel, risk = kit.store.risks[-1]
    assert channel == chan and 0 <= risk["score"] <= 1  # type: ignore[operator]
    codes = [r["code"] for r in risk["reasons"]]  # type: ignore[index]
    assert "sensitive_topic.finance" in codes
    assert res.qa.risk_score == round(risk["score"] * 100)  # type: ignore[operator]
    assert risk["thresholds"] == {"escalate_at": 0.35, "block_at": 0.8}


def test_sensitive_topic_handling_is_per_channel() -> None:
    allow, esc, block = Kit(), Kit(), Kit()
    a = _run(allow, allow.channel(safety={"sensitive": {"finance": "allow"}}))
    e = _run(esc, esc.channel(safety={"sensitive": {"finance": "escalate"}}))
    b = _run(block, block.channel(safety={"sensitive": {"finance": "block"}}))
    assert "sensitive_topic" not in _flags(a) and a.qa.passed
    assert _flags(e)["sensitive_topic"] == "warn" and e.qa.passed
    assert _flags(b)["sensitive_topic"] == "block" and not b.qa.passed
    risk = lambda k: k.store.risks[-1][2]["score"]  # noqa: E731
    assert risk(allow) < risk(esc)


def test_banned_topics_block_even_when_the_owner_supplied_the_idea() -> None:
    kit = Kit()
    chan = kit.channel(safety={"banned_topics": ["index funds"], "sensitive": {"finance": "allow"}})
    res = _run(kit, chan)  # the owner's own idea is about index funds
    assert _flags(res)["banned_topic"] == "block" and not res.qa.passed
    assert res.qa.rework_stage == "scripting"


def test_owner_input_never_bypasses_a_check() -> None:
    with_input, without = Kit(), Kit()
    chan = with_input.channel(safety={"banned_topics": ["index funds"]})
    r1 = _run(with_input, chan)
    c2 = without.channel(safety={"banned_topics": ["index funds"]})
    without.knowledge.seed_approved(
        c2,
        __import__("ytcore.knowledge.models", fromlist=["x"]).KnowledgeKind.PLAYBOOK,
        "- Index funds in plain words",
    )
    r2 = without.produce(without.job(c2), c2, max_rework=0)
    assert not r1.qa.passed and not r2.qa.passed
    reasons = lambda k: {r["code"] for r in k.store.risks[-1][2]["reasons"]}  # noqa: E731
    assert "no_owner_input" in reasons(without) and "no_owner_input" not in reasons(with_input)


def test_high_total_risk_blocks_or_escalates_by_channel_thresholds() -> None:
    kit = Kit()
    chan = kit.channel(safety={"escalate_risk_at": 0.05, "block_risk_at": 1.0})
    res = _run(kit, chan)
    assert _flags(res).get("risk_escalated") == "warn" and res.qa.passed
    strict = Kit()
    chan = strict.channel(safety={"escalate_risk_at": 0.0, "block_risk_at": 0.05})
    res = _run(strict, chan)
    assert _flags(res)["risk_too_high"] == "block" and res.qa.rework_stage == "scripting"


def test_the_model_judges_only_borderline_titles() -> None:
    kit = Kit()
    chan = kit.channel(safety={"sensitive": {"finance": "allow"}})
    _run(kit, chan)
    asked = [r.task for r in kit.llm.requests]
    assert asked.count("qa.brand") == 1
    # The fake writer's title shares only some words with the script: borderline -> asked once.
    assert asked.count("qa.policy") <= 1


def test_cross_channel_duplicates_block_and_no_detail_reaches_a_model() -> None:
    kit = Kit()
    a = kit.channel("chan-a", safety={"sensitive": {"finance": "allow"}})
    b = kit.channel("chan-b", safety={"sensitive": {"finance": "allow"}})
    first = _run(kit, a)
    assert first.qa.passed
    before = len(kit.llm.requests)
    second = _run(kit, b)  # same idea and sources: an identical script and identical media
    flags = _flags(second)
    assert flags["duplicate_script"] == "block" and flags["reused_media"] == "block"
    assert not second.qa.passed
    # Details are for human approvers: the flag evidence names the other channel...
    evidence = " ".join(f["claim"] for f in second.qa.flags)
    assert "chan-a" in evidence
    # ...but the reason (which can reach the script writer) and every prompt never do.
    assert all("chan-a" not in f["reason"] for f in second.qa.flags)
    prompts = " ".join(r.system + r.user for r in kit.llm.requests[before:])
    assert "chan-a" not in prompts
    risk = kit.store.risks[-1][2]
    assert "cross_channel_similarity" in {r["code"] for r in risk["reasons"]}  # type: ignore[index]


def test_a_near_copy_of_the_channels_own_history_is_blocked() -> None:
    kit = Kit()
    chan = kit.channel(safety={"sensitive": {"finance": "allow"}})
    assert _run(kit, chan).qa.passed
    artifacts = kit.p  # second video from the same source repeats the first
    second = _run(kit, chan, idea="Index funds again")
    assert _flags(second)["repetitive_content"] == "block"
    assert "low_novelty" in {r["code"] for r in kit.store.risks[-1][2]["reasons"]}  # type: ignore[index]
    del artifacts


def test_oversized_or_disguised_assets_never_get_stored() -> None:
    kit = Kit()
    chan = kit.channel()
    kit.idea(chan, "Index funds")
    job = kit.job(chan)
    artifacts = kit.p.run_stage(job, chan, "research", {})
    artifacts |= kit.p.run_stage(job, chan, "script", artifacts)

    class Big(FakeImage):
        def generate(self, prompt: str, *, size: str = "1280x720") -> MediaResult:
            return MediaResult(b"\x89PNG" + b"0" * (21 * 1024 * 1024), "image/png", 4, "fake-image")

    kit.p._p.image.generate = Big().generate  # type: ignore[method-assign]
    with pytest.raises(AssetRejected, match="limit"):
        kit.p.run_stage(job, chan, "render", artifacts)
    assert not any(a["name"] == "thumbnail" for a in kit.store.assets)


def test_manifest_cannot_drop_the_synthetic_media_disclosure() -> None:
    kit = Kit()
    chan = kit.channel()
    res = _run(kit, chan, max_rework=2)
    manifest = res.manifest
    assert manifest["metadata"]["contains_synthetic_media"] is True
    ensure_disclosed(manifest["asset_info"], True)
    with pytest.raises(ValueError, match="synthetic"):
        ensure_disclosed(manifest["asset_info"], False)
    assert build_manifest is not None


def test_a_reworded_copy_is_escalated_even_when_not_identical() -> None:
    kit = Kit()
    a = kit.channel("chan-a", safety={"sensitive": {"finance": "allow"}})
    b = kit.channel("chan-b", safety={"sensitive": {"finance": "allow"}})
    _run(kit, a)
    second = _run(kit, b, idea="Index funds for beginners")  # same sources, different hook
    assert _flags(second).get("duplicate_script") in {"warn", "block"}

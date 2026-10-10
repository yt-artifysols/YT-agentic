"""Risk scoring, deterministic policy checks, duplicate detection, asset limits and the secret
scrubber (pure functions, no I/O)."""

from __future__ import annotations

import pytest

from ytcore.agents.memory import HashEmbedder
from ytcore.safety.assets import AssetLimits, AssetRejected, check_asset, check_size
from ytcore.safety.checks import (
    AssetView,
    CheckInput,
    check_content,
    requires_synthetic_disclosure,
)
from ytcore.safety.duplicates import (
    Fingerprints,
    InMemoryFingerprintIndex,
    classify_count,
    classify_script,
    jaccard_estimate,
    minhash,
    shingles,
    text_similarity,
    title_template,
)
from ytcore.safety.policy import DuplicateThresholds, Handling, SafetyPolicy
from ytcore.safety.risk import RightsFinding, RiskInputs, assess
from ytcore.safety.scrub import (
    SecretInInput,
    contains_secret,
    ensure_no_secrets,
    find_secrets,
    scrub,
)

# Secret-shaped test strings are assembled at run time so this file never contains one.
ANTHROPIC = "sk-" + "ant-api03-" + "A1b2C3d4" * 4
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GOOGLE_REFRESH = "1//" + "0gAbCdEf" * 5


def _input(**kw: object) -> CheckInput:
    base: dict[str, object] = {
        "topic": "Index funds",
        "title": "Why index funds beat stock picking",
        "script_text": "Index funds track a market index. Over twenty years most active funds "
        "trailed their benchmark. Diversification lowers risk.",
        "description": "Index funds explained",
        "tags": ("index", "funds"),
        "thumbnail_text": "Index funds explained",
        "assets": (),
        "safety": SafetyPolicy(sensitive={"finance": Handling.ALLOW}),
        "license_ok": lambda ref: ref == "lic_ok",
    }
    base.update(kw)
    return CheckInput(**base)  # type: ignore[arg-type]


def _policy(**kw: object) -> SafetyPolicy:
    """A policy that treats finance as allowed (the sample scripts are about funds)."""
    sensitive = {"finance": Handling.ALLOW} | dict(kw.pop("sensitive", {}))  # type: ignore[call-overload]
    return SafetyPolicy(sensitive=sensitive, **kw)  # type: ignore[arg-type]


def _codes(res: object) -> list[str]:
    return [f.code for f in res.findings]  # type: ignore[attr-defined]


# ----------------------------------------------------------------------------- risk


def test_risk_is_a_noisy_or_with_named_reasons() -> None:
    policy = SafetyPolicy()
    clean = assess(RiskInputs(owner_inputs_used=1), policy)
    assert clean.score == 0 and clean.reasons == () and not clean.escalate
    one = assess(RiskInputs(owner_inputs_used=1, sensitive_topics=("health",)), policy)
    two = assess(RiskInputs(owner_inputs_used=1, sensitive_topics=("health", "minors")), policy)
    assert 0 < one.score < two.score < 1
    assert [r.code for r in two.reasons] == ["sensitive_topic.minors", "sensitive_topic.health"]
    assert two.to_json()["reasons"][0]["contribution"] == 0.4
    assert two.score_100 == round(two.score * 100)


def test_every_signal_only_ever_adds_risk() -> None:
    policy = SafetyPolicy()
    base = RiskInputs(owner_inputs_used=1)
    for worse in (
        RiskInputs(owner_inputs_used=1, total_claims=4, unsourced_claims=4),
        RiskInputs(owner_inputs_used=1, rights=(RightsFinding("missing_license", "x"),)),
        RiskInputs(owner_inputs_used=1, brand_voice_score=0.2),
        RiskInputs(owner_inputs_used=1, history_similarity=0.9),
        RiskInputs(owner_inputs_used=1, cross_channel_similarity=0.9),
        RiskInputs(owner_inputs_used=0),
    ):
        assert assess(worse, policy).score > assess(base, policy).score


def test_owner_input_only_removes_the_provenance_reason() -> None:
    policy = SafetyPolicy()
    risky = {"sensitive_topics": ("minors",), "history_similarity": 0.8, "brand_voice_score": 0.3}
    without = assess(RiskInputs(owner_inputs_used=0, **risky), policy)  # type: ignore[arg-type]
    with_input = assess(RiskInputs(owner_inputs_used=3, **risky), policy)  # type: ignore[arg-type]
    assert "no_owner_input" in [r.code for r in without.reasons]
    assert "no_owner_input" not in [r.code for r in with_input.reasons]
    # Every other reason is untouched: owner input never relaxes a check.
    keep = {r.code: r.contribution for r in without.reasons if r.code != "no_owner_input"}
    assert keep == {r.code: r.contribution for r in with_input.reasons}


def test_thresholds_come_from_the_channel_policy() -> None:
    inputs = RiskInputs(owner_inputs_used=1, sensitive_topics=("minors",))  # 0.4
    assert assess(inputs, SafetyPolicy(escalate_risk_at=0.3)).escalate
    assert not assess(inputs, SafetyPolicy(escalate_risk_at=0.5)).escalate
    assert assess(inputs, SafetyPolicy(escalate_risk_at=0.2, block_risk_at=0.4)).block
    allowed = SafetyPolicy(sensitive={"minors": Handling.ALLOW})
    assert assess(inputs, allowed).score == 0
    with pytest.raises(ValueError, match="escalate_risk_at"):
        SafetyPolicy(escalate_risk_at=0.9, block_risk_at=0.5)
    with pytest.raises(ValueError, match="escalate thresholds"):
        DuplicateThresholds(shingle_escalate=0.9, shingle_block=0.5)


# ----------------------------------------------------------------------------- checks


def test_a_clean_script_has_no_findings() -> None:
    res = check_content(_input())
    assert res.findings == [] and res.borderline == []


def test_banned_topics_are_per_channel_and_blocking() -> None:
    policy = _policy(banned_topics=["lottery"])
    res = check_content(_input(script_text="The lottery is a bad bet.", safety=policy))
    assert _codes(res) == ["banned_topic"] and res.findings[0].severity == "block"
    assert check_content(_input()).findings == []  # other channels are unaffected


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("The vaccine schedule changed.", "health"),
        ("Prices of bitcoin rose.", "finance"),
        ("Voters head to the election.", "politics"),
        ("Teenagers and kids online.", "minors"),
        ("Breaking news from the capital.", "breaking_news"),
    ],
)
def test_sensitive_topics_follow_the_channel_handling(text: str, topic: str) -> None:
    default = check_content(_input(script_text=text, safety=SafetyPolicy()))
    default = check_content(
        _input(script_text=text, safety=_policy(sensitive={topic: Handling.ESCALATE}))
    )
    assert topic in default.sensitive
    assert [f.severity for f in default.findings if f.code == "sensitive_topic"] == ["warn"]
    blocked = check_content(
        _input(script_text=text, safety=_policy(sensitive={topic: Handling.BLOCK}))  # type: ignore[dict-item]
    )
    assert [f.severity for f in blocked.findings if f.code == "sensitive_topic"] == ["block"]
    allowed = check_content(
        _input(script_text=text, safety=_policy(sensitive={topic: Handling.ALLOW}))  # type: ignore[dict-item]
    )
    assert not [f for f in allowed.findings if f.code == "sensitive_topic"]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Get rich quick with guaranteed income.", "scam_pattern"),
        ("This tea cures cancer in a week.", "medical_claim"),
        ("Stop taking your medication today.", "medical_claim"),
        ("As your financial advisor, I recommend this fund.", "ai_expert_persona"),
    ],
)
def test_scams_medical_claims_and_expert_personas_block(text: str, code: str) -> None:
    res = check_content(_input(script_text=text))
    assert code in _codes(res)
    assert all(f.severity == "block" for f in res.findings if f.code == code)


def test_repetitive_and_templated_scripts_are_flagged() -> None:
    repeated = "Buy the thing today for yourself. " * 6
    res = check_content(_input(script_text=repeated))
    assert any(f.code == "repetitive_content" and f.severity == "block" for f in res.findings)
    template = " ".join(f"Here is fact {n} about the topic of saving money." for n in range(6))
    res = check_content(_input(script_text=template))
    assert "mass_produced_content" in _codes(res)
    copy = check_content(_input(history_similarity=0.8))
    assert [f.severity for f in copy.findings if f.code == "repetitive_content"] == ["block"]
    similar = check_content(_input(history_similarity=0.4))
    assert [f.severity for f in similar.findings if f.code == "repetitive_content"] == ["warn"]


def test_misleading_titles_and_thumbnails() -> None:
    res = check_content(_input(title="You won't believe this SHOCKING 10 secrets"))
    assert "misleading_title" in _codes(res)  # clickbait + figures the script never states
    res = check_content(_input(thumbnail_text="ALL MONEY GONE FOREVER"))
    assert "misleading_thumbnail" in _codes(res)
    # The rules cannot settle a partial overlap: that goes to the model.
    res = check_content(
        _input(
            title="Index funds, bonds, cash, stocks and gold compared",
            script_text="Index funds track an index.",
        )
    )
    assert "title" in res.borderline and "misleading_title" not in _codes(res)


def test_license_rules_and_blocked_media() -> None:
    sha = "sha256:" + "1" * 64
    assets = (
        AssetView("music", "music", sha, None, "stock"),
        AssetView("broll-1", "stock", "sha256:" + "2" * 64, "lic_bad", "stock"),
        AssetView("broll-2", "stock", "sha256:" + "3" * 64, "lic_ok", "stock"),
        AssetView("video", "video", "sha256:" + "4" * 64, None, "fake-video"),
    )
    res = check_content(_input(assets=assets))
    assert sorted(_codes(res)) == ["invalid_license", "missing_license"]
    assert {r.code for r in res.rights} == {"missing_license", "invalid_license"}
    blocked = check_content(
        _input(
            assets=(AssetView("video", "video", sha, None, "x"),),
            safety=_policy(blocked_media_sha256=[sha]),
        )
    )
    assert _codes(blocked) == ["blocked_media"]


def test_synthetic_media_disclosure_rule() -> None:
    generated = {"voiceover": {"kind": "audio"}, "video": {"kind": "video"}}
    assert requires_synthetic_disclosure(generated)
    assert requires_synthetic_disclosure({"thumbnail": {"kind": "image"}})
    assert not requires_synthetic_disclosure({"music": {"kind": "music"}, "b": {"kind": "stock"}})


# ----------------------------------------------------------------------------- duplicates

SCRIPT_A = (
    "Index funds track a broad market index and charge low annual fees. Over twenty years most "
    "actively managed funds trailed their benchmark. Diversification lowers the risk of one "
    "company failing and keeps returns close to the market."
)


def _fp(text: str, title: str = "Why index funds win", thumb: str = "Index funds win"):
    return Fingerprints.build(
        script_text=text,
        title=title,
        thumbnail_text=thumb,
        media={"video": "sha256:" + "a" * 64},
        embedder=HashEmbedder(),
    )


def test_shingling_and_minhash_measure_text_overlap() -> None:
    assert text_similarity(SCRIPT_A, SCRIPT_A) == 1.0
    edited = SCRIPT_A.replace("low annual fees", "very small yearly costs")
    assert 0.3 < text_similarity(SCRIPT_A, edited) < 0.9
    assert text_similarity(SCRIPT_A, "A completely different sentence about cooking pasta.") < 0.1
    estimate = jaccard_estimate(minhash(shingles(SCRIPT_A)), minhash(shingles(edited)))
    assert abs(estimate - text_similarity(SCRIPT_A, edited)) < 0.25


def test_title_templates_mask_topic_words() -> None:
    assert title_template("Why index funds beat stock picking") == title_template(
        "Why compound interest rewards patient savers"
    )
    assert title_template("Why index funds beat stock picking") != title_template(
        "Index funds explained"
    )


def test_cross_channel_script_and_media_duplicates() -> None:
    index, t = InMemoryFingerprintIndex(), DuplicateThresholds()
    index.record(job_id="j-a", channel_id="a", fp=_fp(SCRIPT_A))
    # Same channel: not a cross-channel duplicate (repetition inside a channel is handled by
    # memory, not by this index).
    assert index.find(job_id="j-a2", channel_id="a", fp=_fp(SCRIPT_A), thresholds=t).matches == []
    report = index.find(job_id="j-b", channel_id="b", fp=_fp(SCRIPT_A), thresholds=t)
    kinds = {m.kind: m for m in report.matches}
    assert kinds["script"].action == "block" and kinds["script"].other_channel == "a"
    assert kinds["media"].action == "block" and kinds["media"].label == "video"
    assert report.script_similarity == 1.0
    other = index.find(
        job_id="j-c",
        channel_id="c",
        fp=_fp("Pasta needs plenty of salted boiling water and a patient cook.", "Pasta", "Pasta"),
        thresholds=t,
    )
    assert [m.kind for m in other.matches] == [] or all(m.kind != "script" for m in other.matches)


def test_repeated_title_templates_and_thumbnails_escalate_then_block() -> None:
    index = InMemoryFingerprintIndex()
    t = DuplicateThresholds(title_template_escalate=2, title_template_block=4,
                            thumbnail_text_escalate=2, thumbnail_text_block=4)  # fmt: skip
    for n in range(1, 5):
        distinct = f"Entirely separate script number {n} " + "word " * n * 7
        index.record(job_id=f"j{n}", channel_id=f"c{n}", fp=_fp(distinct, thumb="Same thumb"))
        found = index.find(
            job_id="probe", channel_id="probe", fp=_fp("Another unrelated text here today."),
            thresholds=t,
        )  # fmt: skip
        by_kind = {m.kind: m.action for m in found.matches}
        expected = None if n < 2 else "escalate" if n < 4 else "block"
        assert by_kind.get("title_template") == expected
    assert classify_count(1, 2, 4) is None and classify_count(4, 2, 4) == "block"
    assert classify_script(0.2, 0.2, t) is None
    assert classify_script(0.5, 0.0, DuplicateThresholds()) == "escalate"
    assert classify_script(0.0, 0.99, DuplicateThresholds()) == "block"


# ----------------------------------------------------------------------------- assets


def test_asset_limits_reject_oversized_disguised_and_unexpected_files() -> None:
    check_asset("video", "video/mp4", b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)
    with pytest.raises(AssetRejected, match="limit"):
        check_asset("image", "image/png", b"\x89PNG" + b"0" * (21 * 1024 * 1024))
    with pytest.raises(AssetRejected, match="not allowed"):
        check_asset("image", "application/x-msdownload", b"\x89PNG....")
    with pytest.raises(AssetRejected, match="executable"):
        check_asset("video", "video/mp4", b"MZ\x90\x00 pretending to be an mp4")
    with pytest.raises(AssetRejected, match="executable"):
        check_asset("video", "video/mp4", b"\x7fELF\x02\x01\x01")
    with pytest.raises(AssetRejected, match="script"):
        check_asset("music", "audio/mpeg", b"#!/bin/sh\nrm -rf /")
    with pytest.raises(AssetRejected, match="archive"):
        check_asset("stock", "video/mp4", b"PK\x03\x04zipbomb")
    with pytest.raises(AssetRejected, match="markup"):
        check_asset("image", "image/png", b"<svg onload=alert(1)></svg>")
    with pytest.raises(AssetRejected, match="markup"):
        check_asset("image", "image/png", b"\x89PNG\r\n" + b"<script>alert(1)</script>")
    with pytest.raises(AssetRejected, match="empty"):
        check_asset("video", "video/mp4", b"")
    with pytest.raises(AssetRejected, match="unknown"):
        check_asset("firmware", "application/octet-stream", b"x")
    small = AssetLimits(max_bytes={"video": 10}, media_types={"video": frozenset({"video/mp4"})})
    with pytest.raises(AssetRejected):
        check_asset("video", "video/mp4", b"x" * 11, small)
    check_size("video", 100)
    with pytest.raises(AssetRejected):
        check_size("video", 3 * 1024**4)


# ----------------------------------------------------------------------------- scrubber


def test_secret_shapes_are_found_and_redacted() -> None:
    text = f"key {ANTHROPIC}, aws {AWS}, refresh {GOOGLE_REFRESH}, password=hunter2hunter2"
    kinds = {m.name for m in find_secrets(text)}
    assert {"anthropic-key", "aws-access-key", "google-refresh-token", "assigned-secret"} <= kinds
    clean = scrub(text)
    assert not contains_secret(clean) and ANTHROPIC not in clean and AWS not in clean
    assert "[REDACTED:anthropic-key]" in clean


def test_scrub_leaves_normal_text_and_hashes_alone() -> None:
    text = "sha256:" + "a" * 64 + " job 123e4567-e89b-12d3-a456-426614174000 Why index funds win."
    assert scrub(text) == text and not contains_secret(text)


def test_pem_blocks_and_bearer_tokens_are_redacted() -> None:
    pem = "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END " + "PRIVATE KEY-----"
    assert "MIIEvQ" not in scrub(f"oops {pem} done")
    bearer = "Authorization: Bearer " + "abcdEFGH1234" * 3
    assert "abcdEFGH1234" not in scrub(bearer)


def test_ensure_no_secrets_names_the_kind_but_not_the_value() -> None:
    with pytest.raises(SecretInInput) as exc:
        ensure_no_secrets(f"my key is {ANTHROPIC}", "an owner input")
    assert "anthropic-key" in str(exc.value) and ANTHROPIC not in str(exc.value)
    ensure_no_secrets("a perfectly normal sentence", "an owner input")

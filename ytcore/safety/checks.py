"""Deterministic policy checks. Rules first; the model is asked only about borderline cases the
rules cannot settle (misleading title or thumbnail). Rules map to docs/policy-sources.md.

`detail` is generic, channel-local text that may reach the script writer as a revision note;
`evidence` is for human approvers only (it may name other channels' jobs) and never goes into a
prompt."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ytcore.safety.lexicons import (
    AI_EXPERT_PERSONA,
    CLICKBAIT,
    MEDICAL_CLAIM,
    SCAM,
    sensitive_topics,
)
from ytcore.safety.policy import Handling, SafetyPolicy, SensitiveTopic
from ytcore.safety.risk import RightsFinding
from ytcore.safety.textutil import STOP, TOKEN

GENERATED_KINDS = frozenset({"video", "audio", "image"})

Stage = Literal["scripting", "rendering"]


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Literal["warn", "block"]
    detail: str
    evidence: str = ""
    stage: Stage | None = None


@dataclass(frozen=True)
class AssetView:
    name: str
    kind: str
    sha256: str
    license_ref: str | None
    provider: str


@dataclass(frozen=True)
class CheckInput:
    topic: str
    title: str
    script_text: str
    description: str
    tags: tuple[str, ...]
    thumbnail_text: str
    assets: tuple[AssetView, ...]
    safety: SafetyPolicy
    license_ok: Callable[[str], bool]
    history_similarity: float = 0.0


@dataclass
class CheckResult:
    findings: list[Finding] = field(default_factory=list)
    sensitive: list[SensitiveTopic] = field(default_factory=list)
    rights: list[RightsFinding] = field(default_factory=list)
    # Metadata questions the rules cannot settle; the QA agent may ask the model about these.
    borderline: list[Literal["title", "thumbnail"]] = field(default_factory=list)


def content_tokens(text: str) -> set[str]:
    return {t for t in TOKEN.findall(text.lower()) if t not in STOP and len(t) > 2}


def overlap(small: str, big: str) -> float:
    a = content_tokens(small)
    return len(a & content_tokens(big)) / len(a) if a else 1.0


def requires_synthetic_disclosure(asset_info: Mapping[str, Mapping[str, Any]]) -> bool:
    """True when any asset was generated (voice, image or video): it must be disclosed."""
    return any(info.get("kind") in GENERATED_KINDS for info in asset_info.values())


def ensure_disclosed(asset_info: Mapping[str, Mapping[str, Any]], synthetic: object) -> None:
    """Raise if generated media is not declared as synthetic media."""
    if requires_synthetic_disclosure(asset_info) and synthetic is not True:
        raise ValueError("generated media must be disclosed as synthetic media")


def _sentences(text: str) -> list[str]:
    return [s.strip().lower() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 12]


def _banned_topic_rx(topics: list[str]) -> re.Pattern[str] | None:
    phrases = [re.escape(t.strip()) for t in topics if t.strip()]
    return re.compile(r"\b(?:" + "|".join(phrases) + r")\b", re.I) if phrases else None


def check_content(inp: CheckInput) -> CheckResult:
    res = CheckResult()
    add = res.findings.append
    body = f"{inp.title}\n{inp.script_text}\n{inp.description}\n{' '.join(inp.tags)}"
    everything = f"{inp.topic}\n{body}\n{inp.thumbnail_text}"
    policy = inp.safety

    # Banned topics for this channel.
    rx = _banned_topic_rx(policy.banned_topics)
    if rx and (m := rx.search(everything)):
        add(
            Finding(
                "banned_topic",
                "block",
                f"avoid: banned topic '{m.group(0)}'",
                m.group(0),
                "scripting",
            )
        )

    # Sensitive topics, handled per channel.
    res.sensitive = sensitive_topics(f"{inp.topic}\n{body}")
    for topic in res.sensitive:
        handling = policy.handling(topic)
        if handling is Handling.BLOCK:
            add(
                Finding(
                    "sensitive_topic",
                    "block",
                    f"avoid: {topic} content is blocked on this channel",
                    topic,
                    "scripting",
                )
            )
        elif handling is Handling.ESCALATE:
            add(
                Finding(
                    "sensitive_topic", "warn", f"touches {topic}; escalated for human review", topic
                )
            )

    # Scams, dangerous medical claims and AI personas posing as experts.
    for code, rule, why in (
        ("scam_pattern", SCAM, "avoid: scam-style promises"),
        ("medical_claim", MEDICAL_CLAIM, "avoid: unsupported medical claims"),
        ("ai_expert_persona", AI_EXPERT_PERSONA, "avoid: presenting as a human expert"),
    ):
        if m := rule.search(everything):
            add(Finding(code, "block", why, m.group(0), "scripting"))

    # Repetitious and mass-produced content.
    sentences = _sentences(inp.script_text)
    if len(sentences) >= 4:
        repeated = 1 - len(set(sentences)) / len(sentences)
        if repeated >= 0.5:
            add(
                Finding(
                    "repetitive_content",
                    "block",
                    "avoid: repeating the same sentences",
                    f"{repeated:.0%} repeated",
                    "scripting",
                )
            )
        elif repeated > policy.max_repeated_sentence_ratio:
            add(
                Finding(
                    "repetitive_content",
                    "warn",
                    "script repeats itself",
                    f"{repeated:.0%} repeated",
                )
            )
    openings = [" ".join(s.split()[:3]) for s in sentences if len(s.split()) >= 3]
    if len(openings) >= 4 and max(openings.count(o) for o in set(openings)) / len(openings) >= 0.7:
        add(
            Finding(
                "mass_produced_content",
                "warn",
                "script reads like a template",
                "same opening in most sentences",
            )
        )
    toks = TOKEN.findall(inp.script_text.lower())
    if len(toks) >= 120 and len(set(toks)) / len(toks) < 0.25:
        add(
            Finding(
                "mass_produced_content",
                "warn",
                "very low vocabulary diversity",
                f"{len(set(toks))}/{len(toks)} distinct words",
            )
        )
    t = policy.duplicates
    if inp.history_similarity >= t.shingle_block:
        add(
            Finding(
                "repetitive_content",
                "block",
                "avoid: near copy of an earlier video on this channel",
                f"{inp.history_similarity:.2f}",
                "scripting",
            )
        )
    elif inp.history_similarity >= t.shingle_escalate:
        add(
            Finding(
                "repetitive_content",
                "warn",
                "similar to an earlier video on this channel",
                f"{inp.history_similarity:.2f}",
            )
        )

    # Misleading titles and thumbnails.
    for label, code, text in (
        ("title", "misleading_title", inp.title),
        ("thumbnail", "misleading_thumbnail", inp.thumbnail_text),
    ):
        if not text.strip():
            continue
        if CLICKBAIT.search(text):
            add(Finding(code, "warn", f"{label} uses clickbait phrasing", text))
        letters = [c for c in text if c.isalpha()]
        if len(letters) > 8 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
            add(Finding(code, "warn", f"{label} is mostly capital letters", text))
        script_numbers = set(re.findall(r"\d+", f"{inp.script_text} {inp.description}"))
        missing = [n for n in re.findall(r"\d+", text) if n not in script_numbers]
        if missing:
            add(
                Finding(
                    code,
                    "warn",
                    f"{label} promises figures the script never states",
                    ", ".join(missing),
                )
            )
        share = overlap(text, f"{inp.script_text} {inp.description}")
        if share < 0.1:
            add(
                Finding(
                    code,
                    "warn",
                    f"{label} has almost nothing in common with the script",
                    f"{share:.2f}",
                )
            )
        elif share < 0.35:
            res.borderline.append(label)  # type: ignore[arg-type]

    # Rights: licenses for music and stock, and known third-party media.
    blocked = set(policy.blocked_media_sha256)
    for asset in inp.assets:
        if asset.sha256 in blocked:
            res.rights.append(
                RightsFinding("blocked_media", f"{asset.name} is on the blocked media list")
            )
            add(
                Finding(
                    "blocked_media",
                    "block",
                    f"{asset.name} is known third-party media",
                    asset.sha256,
                    "rendering",
                )
            )
        if asset.kind in ("music", "stock"):
            if not asset.license_ref:
                res.rights.append(
                    RightsFinding("missing_license", f"{asset.name} has no license_ref")
                )
                add(
                    Finding(
                        "missing_license",
                        "block",
                        f"{asset.name} has no license_ref",
                        asset.name,
                        "rendering",
                    )
                )
            elif not inp.license_ok(asset.license_ref):
                res.rights.append(
                    RightsFinding("invalid_license", f"{asset.name} has an invalid license")
                )
                add(
                    Finding(
                        "invalid_license",
                        "block",
                        f"{asset.name} has no valid license",
                        asset.license_ref,
                        "rendering",
                    )
                )
    return res

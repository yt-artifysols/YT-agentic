"""Risk scoring: a 0..1 score with named reasons, stored on the job.

Every signal contributes a probability-like value c_i in 0..1; the score is the noisy-or
1 - prod(1 - c_i), so more reasons never lower the score and the score stays within 0..1.
Provenance only ever ADDS risk (a video generated without any owner input is a mass-production
signal); having owner inputs removes that one reason and never relaxes any other check or
reason. Thresholds come from the channel's safety policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import prod
from typing import Any

from ytcore.safety.lexicons import SENSITIVE_WEIGHT
from ytcore.safety.policy import Handling, SafetyPolicy, SensitiveTopic


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass(frozen=True)
class RightsFinding:
    code: str  # missing_license | invalid_license | blocked_media | license_expiring
    detail: str


@dataclass(frozen=True)
class RiskInputs:
    total_claims: int = 0
    unsourced_claims: int = 0
    sensitive_topics: tuple[SensitiveTopic, ...] = ()
    rights: tuple[RightsFinding, ...] = ()
    brand_voice_score: float = 1.0
    # Highest word-shingle similarity to this channel's own earlier scripts (0 = all new).
    history_similarity: float = 0.0
    owner_inputs_used: int = 0
    # Highest similarity to ANOTHER channel's script (cross-channel duplicate detector).
    cross_channel_similarity: float = 0.0
    policy_warnings: int = 0


@dataclass(frozen=True)
class RiskReason:
    code: str
    contribution: float  # 0..1
    detail: str


@dataclass(frozen=True)
class RiskAssessment:
    score: float
    reasons: tuple[RiskReason, ...]
    escalate: bool
    block: bool
    thresholds: dict[str, float] = field(default_factory=dict)

    @property
    def score_100(self) -> int:
        """The 0..100 integer the approval hub sorts by."""
        return round(self.score * 100)

    def to_json(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "escalate": self.escalate,
            "block": self.block,
            "reasons": [asdict(r) for r in self.reasons],
            "thresholds": self.thresholds,
        }


RIGHTS_WEIGHT = {
    "missing_license": 0.6,
    "invalid_license": 0.6,
    "blocked_media": 0.9,
    "license_expiring": 0.15,
}


def assess(inputs: RiskInputs, policy: SafetyPolicy) -> RiskAssessment:
    reasons: list[RiskReason] = []

    def add(code: str, value: float, detail: str) -> None:
        if value > 0.0005:
            reasons.append(RiskReason(code, round(_clamp(value), 4), detail))

    if inputs.total_claims:
        ratio = inputs.unsourced_claims / inputs.total_claims
        add(
            "unsourced_claims",
            0.5 * ratio,
            f"{inputs.unsourced_claims} of {inputs.total_claims} factual claims cite no source",
        )
    for topic in inputs.sensitive_topics:
        handling = policy.handling(topic)
        if handling is not Handling.ALLOW:
            add(f"sensitive_topic.{topic}", SENSITIVE_WEIGHT[topic], f"touches {topic}")
    for finding in inputs.rights:
        add(f"rights.{finding.code}", RIGHTS_WEIGHT.get(finding.code, 0.3), finding.detail)
    add(
        "brand_voice_drift",
        0.35 * _clamp((0.8 - inputs.brand_voice_score) / 0.8),
        f"brand voice score {inputs.brand_voice_score:.2f}",
    )
    add(
        "low_novelty",
        0.4 * _clamp((inputs.history_similarity - 0.2) / 0.6),
        f"{inputs.history_similarity:.2f} similar to this channel's earlier scripts",
    )
    add(
        "cross_channel_similarity",
        0.7 * _clamp((inputs.cross_channel_similarity - 0.2) / 0.6),
        f"{inputs.cross_channel_similarity:.2f} similar to another channel's script",
    )
    if inputs.owner_inputs_used == 0:
        add(
            "no_owner_input",
            0.10,
            "generated without any owner input (mass-produced content is a policy risk)",
        )
    add(
        "policy_warnings",
        min(0.3, 0.05 * inputs.policy_warnings),
        f"{inputs.policy_warnings} policy warnings",
    )

    reasons.sort(key=lambda r: -r.contribution)
    score = 1.0 - prod(1.0 - r.contribution for r in reasons)
    return RiskAssessment(
        score=_clamp(score),
        reasons=tuple(reasons),
        escalate=score >= policy.escalate_risk_at,
        block=score >= policy.block_risk_at,
        thresholds={"escalate_at": policy.escalate_risk_at, "block_at": policy.block_risk_at},
    )

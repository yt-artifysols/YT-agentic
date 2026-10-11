"""Tier rules (from config/platform.yaml) and the pure promotion / demotion assessments.

Tier 1 = approve-all. Tier 2 = risk-based: low-risk videos go to a batch fast lane (still one
human-issued token per video), the rest get deep review, and a share of low-risk videos is
routed to deep review anyway (spot checks). Nothing here touches the approval gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PromotionRules(_Strict):
    # A channel earns a RECOMMENDATION when all of these hold; an admin must confirm it.
    min_days: int = Field(default=30, ge=1, le=365, description="days since its first decision")
    min_decisions: int = Field(default=20, ge=1)
    max_rejection_rate: float = Field(default=0.1, ge=0, le=1)
    max_edit_rate: float = Field(default=0.25, ge=0, le=1)
    max_avg_risk: int = Field(default=35, ge=0, le=100)
    max_cost_per_video_cents: int | None = Field(default=None, ge=0)
    strike_free_days: int = Field(default=90, ge=1)
    max_policy_flags: int = Field(default=0, ge=0)


class DemotionRules(_Strict):
    # Any one of these returns the channel to tier 1 immediately.
    window_decisions: int = Field(default=10, ge=1)
    max_rejections_in_window: int = Field(default=3, ge=1)
    policy_flag_window_days: int = Field(default=14, ge=1)
    max_policy_flags: int = Field(default=0, ge=0)


class TierRules(_Strict):
    spot_check_percent: float = Field(default=10.0, ge=0, le=100)
    promotion: PromotionRules = Field(default_factory=PromotionRules)
    demotion: DemotionRules = Field(default_factory=DemotionRules)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class ChannelStats:
    """What the rules look at, measured over the relevant windows (see TierService.stats)."""

    days_observed: float = 0.0
    decisions: int = 0
    rejected: int = 0
    edited: int = 0
    avg_risk: float | None = None
    cost_per_video_cents: float | None = None
    strikes: int = 0
    policy_flags: int = 0
    recent_decisions: int = 0
    recent_rejections: int = 0
    recent_policy_flags: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def evidence(self) -> dict[str, Any]:
        return {
            "days_observed": round(self.days_observed, 1),
            "decisions": self.decisions,
            "rejected": self.rejected,
            "edited": self.edited,
            "avg_risk": None if self.avg_risk is None else round(self.avg_risk, 1),
            "cost_per_video_cents": (
                None if self.cost_per_video_cents is None else round(self.cost_per_video_cents, 1)
            ),
            "strikes": self.strikes,
            "policy_flags": self.policy_flags,
        }


def assess_promotion(stats: ChannelStats, rules: TierRules) -> list[str]:
    """Reasons the channel is NOT yet eligible for a recommendation (empty = eligible)."""
    p = rules.promotion
    problems: list[str] = []
    if stats.days_observed < p.min_days:
        problems.append(f"only {stats.days_observed:.0f} of {p.min_days} days observed")
    if stats.decisions < p.min_decisions:
        problems.append(f"only {stats.decisions} of {p.min_decisions} decisions")
    if stats.decisions:
        if stats.rejected / stats.decisions > p.max_rejection_rate:
            problems.append(f"rejection rate {stats.rejected / stats.decisions:.0%} too high")
        if stats.edited / stats.decisions > p.max_edit_rate:
            problems.append(f"edit rate {stats.edited / stats.decisions:.0%} too high")
    if stats.avg_risk is not None and stats.avg_risk > p.max_avg_risk:
        problems.append(f"average risk {stats.avg_risk:.0f} above {p.max_avg_risk}")
    if (
        p.max_cost_per_video_cents is not None
        and stats.cost_per_video_cents is not None
        and stats.cost_per_video_cents > p.max_cost_per_video_cents
    ):
        problems.append(f"cost per video {stats.cost_per_video_cents:.0f}c above the limit")
    if stats.strikes:
        problems.append(f"{stats.strikes} strike(s) in the last {p.strike_free_days} days")
    if stats.policy_flags > p.max_policy_flags:
        problems.append(f"{stats.policy_flags} policy flag(s) in the window")
    return problems


def assess_demotion(stats: ChannelStats, rules: TierRules) -> str | None:
    """The reason to return a tier-2 channel to approve-all, or None to leave it."""
    d = rules.demotion
    if stats.strikes:
        return f"{stats.strikes} strike(s) recorded"
    if stats.recent_rejections >= d.max_rejections_in_window:
        return (
            f"rejection spike: {stats.recent_rejections} of the last "
            f"{stats.recent_decisions} decisions"
        )
    if stats.recent_policy_flags > d.max_policy_flags:
        return (
            f"{stats.recent_policy_flags} policy flag(s) in the last "
            f"{d.policy_flag_window_days} days"
        )
    return None

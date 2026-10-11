"""Pure tier logic: promotion needs a clean record, demotion triggers, and structural safety."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ytcore.tiers.rules import ChannelStats, TierRules, assess_demotion, assess_promotion

ROOT = Path(__file__).resolve().parents[2]
RULES = TierRules()
GOOD = ChannelStats(
    days_observed=31, decisions=25, rejected=1, edited=2, avg_risk=18.0, cost_per_video_cents=40
)


def test_a_clean_record_is_eligible_for_a_recommendation() -> None:
    assert assess_promotion(GOOD, RULES) == []


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"days_observed": 12}, "days observed"),
        ({"decisions": 5}, "decisions"),
        ({"rejected": 5}, "rejection rate"),
        ({"edited": 12}, "edit rate"),
        ({"avg_risk": 60.0}, "average risk"),
        ({"cost_per_video_cents": 400.0}, "cost per video"),
        ({"strikes": 1}, "strike"),
        ({"policy_flags": 1}, "policy flag"),
    ],
)
def test_each_promotion_rule_blocks(change: dict[str, float], fragment: str) -> None:
    stats = ChannelStats(**{**GOOD.__dict__, **change})
    rules = TierRules.model_validate({"promotion": {"max_cost_per_video_cents": 100}})
    assert any(fragment in p for p in assess_promotion(stats, rules))


def test_a_quiet_tier_two_channel_stays() -> None:
    assert assess_demotion(ChannelStats(recent_decisions=10, recent_rejections=1), RULES) is None


@pytest.mark.parametrize(
    ("stats", "fragment"),
    [
        (ChannelStats(strikes=1), "strike"),
        (ChannelStats(recent_decisions=10, recent_rejections=3), "rejection spike"),
        (ChannelStats(recent_policy_flags=1), "policy flag"),
    ],
)
def test_each_demotion_trigger_fires(stats: ChannelStats, fragment: str) -> None:
    assert fragment in (assess_demotion(stats, RULES) or "")


def test_rules_reject_unknown_keys_and_out_of_range_values() -> None:
    with pytest.raises(ValueError):
        TierRules.model_validate({"auto_promote": True})
    with pytest.raises(ValueError):
        TierRules.model_validate({"spot_check_percent": 150})


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def test_tiers_are_not_part_of_any_publish_path() -> None:
    """No new publish path: the gateway and the token code never read tiers, and the tier
    package can neither sign, verify nor upload."""
    for path in (ROOT / "ytcore/gateway").rglob("*.py"):
        assert not any(m.startswith("ytcore.tiers") for m in _imports(path)), path
    forbidden = ("ytcore.gateway", "ytcore.approvals.keys", "ytcore.approvals.token")
    for path in (ROOT / "ytcore/tiers").rglob("*.py"):
        assert not any(m.startswith(forbidden) for m in _imports(path)), path
    sql = (ROOT / "ytcore/db/migrations/013_autonomy_tiers.sql").read_text()
    assert "approvals" not in sql.replace("hub_users", "")  # the approval gate is untouched


def test_the_production_hub_refuses_a_blind_in_memory_asset_store() -> None:
    from ytcore.approvals.service.main import resolve_assets
    from ytcore.approvals.service.settings import HubSettings
    from ytcore.gateway.assets import InMemoryAssetSource

    prod = HubSettings.model_construct(env="production")
    with pytest.raises(RuntimeError, match="real asset source"):
        resolve_assets(prod, None)
    real = InMemoryAssetSource()
    assert resolve_assets(prod, real) is real
    assert isinstance(
        resolve_assets(HubSettings.model_construct(env="dev"), None), InMemoryAssetSource
    )

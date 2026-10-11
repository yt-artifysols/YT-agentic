"""Fleet configuration: schema, cross-file rules, and that the shipped examples stay valid."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from ytcore.channels.config import (
    CONFIG_ROOT,
    ChannelCfg,
    ConfigError,
    load_fleet,
)
from ytcore.knowledge.models import KnowledgeKind

EXAMPLES = CONFIG_ROOT / "examples"


@pytest.fixture
def fleet_dir(tmp_path: Path) -> Path:
    shutil.copytree(EXAMPLES, tmp_path / "cfg")
    return tmp_path / "cfg"


def _edit(path: Path, **changes: object) -> None:
    data = yaml.safe_load(path.read_text())
    data.update(changes)
    path.write_text(yaml.safe_dump(data))


def test_the_shipped_examples_load() -> None:
    fleet = load_fleet(EXAMPLES)
    assert set(fleet.clusters) == {"cluster-a", "cluster-b"}
    assert set(fleet.channels) == {"finance-daily", "diy-weekly"}
    assert fleet.clusters["cluster-b"].requires_cluster == "cluster-a"


def test_credentials_are_derived_vault_references_never_values() -> None:
    ch = load_fleet(EXAMPLES).channels["finance-daily"]
    assert ch.youtube_credentials_ref == "vault://youtube-refresh-token-finance-daily"
    assert ch.analytics_credentials_ref == "vault://youtube-analytics-token-finance-daily"


def test_brand_and_policy_become_knowledge_documents() -> None:
    docs = load_fleet(EXAMPLES).channels["finance-daily"].knowledge()
    assert {KnowledgeKind.BRAND_VOICE, KnowledgeKind.HOOK_LIBRARY, KnowledgeKind.POLICY} <= set(
        docs
    )
    assert '"banned_topics"' in docs[KnowledgeKind.POLICY]


def test_unknown_fields_are_rejected(fleet_dir: Path) -> None:
    _edit(fleet_dir / "channels" / "finance-daily.yaml", autopublish=True)
    with pytest.raises(ValueError, match="autopublish"):
        load_fleet(fleet_dir)


def test_there_is_no_auto_publish_tier(fleet_dir: Path) -> None:
    _edit(fleet_dir / "channels" / "finance-daily.yaml", autonomy_tier="auto")
    with pytest.raises(ValueError, match="autonomy_tier"):
        load_fleet(fleet_dir)


def test_file_name_must_match_the_slug(fleet_dir: Path) -> None:
    (fleet_dir / "channels" / "finance-daily.yaml").rename(fleet_dir / "channels" / "other.yaml")
    with pytest.raises(ConfigError, match="file name"):
        load_fleet(fleet_dir)


def test_unknown_cluster_and_time_zone_are_rejected(fleet_dir: Path) -> None:
    _edit(fleet_dir / "channels" / "diy-weekly.yaml", cluster="nope")
    with pytest.raises(ConfigError, match="unknown cluster"):
        load_fleet(fleet_dir)
    _edit(
        fleet_dir / "channels" / "diy-weekly.yaml", cluster="cluster-b", audience_timezone="Mars/X"
    )
    with pytest.raises(ValueError, match="time zone"):
        load_fleet(fleet_dir)


def test_a_dark_cluster_cannot_hold_a_gated_channel(fleet_dir: Path) -> None:
    _edit(fleet_dir / "channels" / "diy-weekly.yaml", autonomy_tier="gated")
    with pytest.raises(ConfigError, match="not live yet"):
        load_fleet(fleet_dir)


def test_a_cluster_with_a_prerequisite_cannot_start_live(fleet_dir: Path) -> None:
    _edit(fleet_dir / "clusters" / "cluster-b.yaml", live=True)
    with pytest.raises(ConfigError, match="live must be false"):
        load_fleet(fleet_dir)


def test_rollout_cycles_and_unknown_prerequisites_are_rejected(fleet_dir: Path) -> None:
    _edit(fleet_dir / "clusters" / "cluster-a.yaml", requires_cluster="cluster-b", live=False)
    with pytest.raises(ConfigError, match="cycle"):
        load_fleet(fleet_dir)
    _edit(fleet_dir / "clusters" / "cluster-a.yaml", requires_cluster="ghost", live=False)
    with pytest.raises(ConfigError, match="unknown cluster"):
        load_fleet(fleet_dir)


def test_quota_arithmetic_is_checked_per_google_project(tmp_path: Path) -> None:
    root = tmp_path / "cfg"
    (root / "clusters").mkdir(parents=True)
    (root / "channels").mkdir()
    cluster = yaml.safe_load((EXAMPLES / "clusters" / "cluster-a.yaml").read_text())
    cluster["google_projects"][0]["daily_quota_units"] = 10_000
    (root / "clusters" / "cluster-a.yaml").write_text(yaml.safe_dump(cluster))
    for i in range(6):  # 6 uploads x 1600 = 9600 > 80% of 10000
        (root / "channels" / f"chan-{i}.yaml").write_text(
            yaml.safe_dump({"slug": f"chan-{i}", "display_name": "x", "cluster": "cluster-a"})
        )
    with pytest.raises(ConfigError, match="usable"):
        load_fleet(root)


def test_the_twenty_channel_cap(tmp_path: Path) -> None:
    root = tmp_path / "cfg"
    (root / "clusters").mkdir(parents=True)
    (root / "channels").mkdir()
    for c in range(5):
        (root / "clusters" / f"cl-{c}.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": f"cl-{c}",
                    "display_name": "c",
                    "live": True,
                    "google_projects": [{"id": f"proj-aaa-{c}", "daily_quota_units": 10_000}],
                }
            )
        )
    for i in range(21):
        (root / "channels" / f"ch-{i:02}.yaml").write_text(
            yaml.safe_dump({"slug": f"ch-{i:02}", "display_name": "x", "cluster": f"cl-{i % 5}"})
        )
    with pytest.raises(ConfigError, match="exceed the limit of 20"):
        load_fleet(root)


def test_credential_shaped_strings_are_rejected(fleet_dir: Path) -> None:
    secret = "sk-ant-" + "a1B2c3D4" * 4
    _edit(fleet_dir / "channels" / "finance-daily.yaml", display_name=f"key {secret}")
    with pytest.raises(ConfigError, match="credential"):
        load_fleet(fleet_dir)


def test_job_budget_cannot_exceed_the_daily_budget() -> None:
    with pytest.raises(ValueError, match="job_cents"):
        ChannelCfg.model_validate(
            {
                "slug": "abc",
                "display_name": "x",
                "cluster": "c",
                "budget": {"job_cents": 5000, "daily_cents": 100},
            }
        )

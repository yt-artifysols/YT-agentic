"""Applying the fleet configuration to Postgres: idempotent, safe, no code changes per channel."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from tests.integration.conftest import fresh_database
from ytcore.channels.apply import apply_fleet
from ytcore.channels.config import CONFIG_ROOT, ConfigError, load_fleet
from ytcore.store.errors import RolloutRefused

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


@pytest.fixture
def db() -> Any:
    with fresh_database() as conninfo:
        yield conninfo


def _connect(db: str) -> Callable[[], psycopg.Connection[Any]]:
    return lambda: psycopg.connect(db, autocommit=True)


def _sql(db: str, query: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(db, autocommit=True) as conn:
        cur = conn.execute(query, params or None)
        return cur.fetchall() if cur.description else []


def test_apply_creates_everything_then_changes_nothing(db: str) -> None:
    fleet = load_fleet(CONFIG_ROOT / "examples")
    first = apply_fleet(_connect(db), fleet)
    assert first.clusters == {"cluster-a": "created", "cluster-b": "created"}
    assert first.channels == {"diy-weekly": "created", "finance-daily": "created"}
    assert first.changed

    again = apply_fleet(_connect(db), fleet)
    assert not again.changed
    assert set(again.channels.values()) == {"unchanged"}
    assert _sql(db, "SELECT count(*) FROM channels")[0][0] == 2
    # the same drafts are not proposed twice
    assert _sql(db, "SELECT count(*) FROM knowledge_docs WHERE proposed_by LIKE 'config:%%'")[0][
        0
    ] == sum(len(c.knowledge()) for c in fleet.channels.values())


def test_channel_rows_follow_the_config(db: str) -> None:
    apply_fleet(_connect(db), load_fleet(CONFIG_ROOT / "examples"))
    row = _sql(
        db,
        "SELECT cluster_id, autonomy_tier, audience_timezone, publish_hour_local, "
        "publish_window_minutes, daily_budget_cents, job_budget_cents, storage_prefix, "
        "memory_namespace, youtube_credentials_ref, google_project_id FROM channels "
        "WHERE id = 'finance-daily'",
    )[0]
    assert row == (
        "cluster-a", "shadow", "America/New_York", 17, 120, 1500, 300, "channels/finance-daily/",
        "mem:finance-daily", "vault://youtube-refresh-token-finance-daily", "ytcore-cluster-a-1",
    )  # fmt: skip
    assert _sql(db, "SELECT live_enabled FROM clusters ORDER BY id") == [(True,), (False,)]
    assert _sql(db, "SELECT pool, max_concurrent FROM pool_limits WHERE scope_id = 'cluster-a' "
                "ORDER BY pool") == [("gpu_render", 1), ("media", 2), ("qa", 3), ("research", 3),
                                     ("script_seo", 3)]  # fmt: skip


def test_brand_and_policy_are_drafts_a_human_must_approve(db: str) -> None:
    apply_fleet(_connect(db), load_fleet(CONFIG_ROOT / "examples"))
    rows = _sql(db, "SELECT status, proposed_by FROM knowledge_docs")
    assert rows and {r[0] for r in rows} == {"draft"}
    assert all(r[1].startswith("config:") for r in rows)


def test_dry_run_writes_nothing(db: str) -> None:
    report = apply_fleet(_connect(db), load_fleet(CONFIG_ROOT / "examples"), dry_run=True)
    assert report.dry_run and report.channels["finance-daily"] == "created"
    assert _sql(db, "SELECT count(*) FROM channels")[0][0] == 0
    assert _sql(db, "SELECT count(*) FROM clusters WHERE id LIKE 'cluster-%%'")[0][0] == 0


def test_edits_update_the_row_and_adding_a_channel_needs_only_a_file(
    db: str, tmp_path: Path
) -> None:
    cfg = tmp_path / "cfg"
    shutil.copytree(CONFIG_ROOT / "examples", cfg)
    apply_fleet(_connect(db), load_fleet(cfg))

    path = cfg / "channels" / "finance-daily.yaml"
    data = yaml.safe_load(path.read_text())
    data["budget"]["daily_cents"] = 2500
    path.write_text(yaml.safe_dump(data))
    (cfg / "channels" / "markets-weekly.yaml").write_text(
        yaml.safe_dump(
            {"slug": "markets-weekly", "display_name": "Markets", "cluster": "cluster-a"}
        )
    )
    report = apply_fleet(_connect(db), load_fleet(cfg))
    assert report.channels["finance-daily"] == "updated"
    assert report.channels["markets-weekly"] == "created"
    assert report.channels["diy-weekly"] == "unchanged"
    assert _sql(db, "SELECT daily_budget_cents FROM channels WHERE id = 'finance-daily'") == [
        (2500,)
    ]


def test_changed_brand_text_becomes_a_new_draft_not_an_edit_of_approved_knowledge(
    db: str, tmp_path: Path
) -> None:
    cfg = tmp_path / "cfg"
    shutil.copytree(CONFIG_ROOT / "examples", cfg)
    apply_fleet(_connect(db), load_fleet(cfg))
    path = cfg / "channels" / "finance-daily.yaml"
    data = yaml.safe_load(path.read_text())
    data["brand"]["brand_voice"] = "Warm and direct.\n"
    path.write_text(yaml.safe_dump(data))
    report = apply_fleet(_connect(db), load_fleet(cfg))
    assert report.drafts["finance-daily"]["brand_voice"] == "proposed"
    versions = _sql(db, "SELECT version FROM knowledge_docs WHERE channel_id = 'finance-daily' "
                    "AND kind = 'brand_voice' ORDER BY version")  # fmt: skip
    assert versions == [(1,), (2,)]


def test_a_channel_cannot_be_moved_to_another_cluster(db: str, tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    shutil.copytree(CONFIG_ROOT / "examples", cfg)
    apply_fleet(_connect(db), load_fleet(cfg))
    path = cfg / "channels" / "diy-weekly.yaml"
    data = yaml.safe_load(path.read_text())
    data["cluster"] = "cluster-a"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="another cluster"):
        apply_fleet(_connect(db), load_fleet(cfg))


def test_config_cannot_switch_a_dark_cluster_live(db: str) -> None:
    apply_fleet(_connect(db), load_fleet(CONFIG_ROOT / "examples"))
    # the database refuses a gated channel in a dark cluster even if someone bypasses the loader
    with pytest.raises(psycopg.Error) as exc:
        _sql(db, "UPDATE channels SET autonomy_tier = 'gated' WHERE id = 'diy-weekly'")
    from ytcore.store.postgres import map_db_error

    assert isinstance(map_db_error(exc.value), RolloutRefused)

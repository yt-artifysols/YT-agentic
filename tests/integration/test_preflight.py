"""Go-live preflight: read-only checks that block go-live until a channel is really ready."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from tests.integration.conftest import fresh_database
from tests.scalekit import write_fleet
from ytcore.approvals.service.repo import HubRepository
from ytcore.channels.apply import apply_fleet
from ytcore.channels.config import load_fleet
from ytcore.ops import preflight, reconcile
from ytcore.secrets.provider import InMemorySecretsProvider

pytestmark = pytest.mark.integration


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


def _by_name(checks: list[preflight.Check]) -> dict[str, preflight.Check]:
    return {c.name: c for c in checks}


def _ready_fleet(db: str, tmp_path: Path) -> tuple[list[str], dict[str, str]]:
    slugs = write_fleet(tmp_path / "cfg", clusters=1, per_cluster=2)
    fleet = load_fleet(tmp_path / "cfg")
    apply_fleet(_connect(db), fleet)
    hub = HubRepository.from_conninfo(db)
    hub.create_user("pf-admin", "Admin", "approver", actor="hub:test")
    for doc_id, sha in _sql(db, "SELECT id::text, content_sha256 FROM knowledge_docs"):
        hub.decide_knowledge(doc_id, sha, username="pf-admin", approve=True)
    values = {ch.youtube_credentials_ref: "dummy" for ch in fleet.channels.values()}
    return slugs, values


def test_an_empty_system_passes_with_a_shadow_mode_warning(db: str) -> None:
    checks = preflight.run(_connect(db))
    assert preflight.go(checks)
    assert _by_name(checks)["gated channels"].status == "warn"
    assert _by_name(checks)["migrations applied"].status == "ok"
    assert _by_name(checks)["audit chain intact"].status == "ok"


def test_a_gated_channel_without_approved_knowledge_or_credentials_blocks_go_live(
    db: str, tmp_path: Path
) -> None:
    slugs, values = _ready_fleet(db, tmp_path)
    # drop one credential: that channel fails, the other passes
    gone = f"vault://youtube-refresh-token-{slugs[0]}"
    secrets = InMemorySecretsProvider({k: v for k, v in values.items() if k != gone})
    checks = _by_name(preflight.run(_connect(db), secrets=secrets))
    assert checks[f"channel {slugs[0]} ready"].status == "fail"
    assert "does not resolve" in checks[f"channel {slugs[0]} ready"].detail
    assert checks[f"channel {slugs[1]} ready"].status == "ok"
    assert "dummy" not in preflight.render(list(checks.values()))  # never prints secrets
    assert not preflight.go(list(checks.values()))


def test_a_fully_ready_fleet_passes_and_an_engaged_kill_switch_only_warns(
    db: str, tmp_path: Path
) -> None:
    _, values = _ready_fleet(db, tmp_path)
    secrets = InMemorySecretsProvider(values)
    checks = preflight.run(_connect(db), secrets=secrets)
    assert preflight.go(checks), preflight.render(checks)
    _sql(db, "UPDATE global_controls SET publishing_paused = true")
    again = _by_name(preflight.run(_connect(db), secrets=secrets))
    assert again["global kill switch"].status == "warn"
    assert preflight.go(list(again.values()))


def test_unapproved_knowledge_blocks(db: str, tmp_path: Path) -> None:
    _, values = _ready_fleet(db, tmp_path)
    # a new channel arrives with only drafts
    fleet = load_fleet(tmp_path / "cfg")
    extra = next(iter(fleet.channels.values())).model_copy(update={"slug": "late-comer"})
    fleet.channels["late-comer"] = extra
    apply_fleet(_connect(db), fleet)
    secrets = InMemorySecretsProvider(
        {**values, "vault://youtube-refresh-token-late-comer": "dummy"}
    )
    checks = _by_name(preflight.run(_connect(db), secrets=secrets))
    assert checks["channel late-comer ready"].status == "fail"
    assert "unapproved knowledge" in checks["channel late-comer ready"].detail


def test_a_pending_upload_blocks_go_live(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, values = _ready_fleet(db, tmp_path)
    pending = reconcile.PendingUpload("u1", "j1", "c1", datetime.now(UTC), "k1")
    monkeypatch.setattr(reconcile, "pending_uploads", lambda _connect: [pending])
    checks = _by_name(preflight.run(_connect(db), secrets=InMemorySecretsProvider(values)))
    assert checks["no uploads left pending"].status == "fail"
    assert not preflight.go(list(checks.values()))

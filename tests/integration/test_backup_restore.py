"""Backup and restore drill against real Postgres and a directory-backed asset store."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from tests.fakes import fake_secrets
from tests.harness import Keys, drive_to_awaiting, make_service
from ytcore.gateway.assets import InMemoryAssetSource, LocalDirAssetSource
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.ops import backup
from ytcore.store.postgres import PostgresStore
from ytcore.workflow.states import JobState

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


@pytest.fixture
def populated(
    database: str, store: PostgresStore, keys: Keys, channel: Callable[..., str], tmp_path: Path
) -> tuple[str, LocalDirAssetSource]:
    """A database with a published video and some audit history, and its asset store."""
    assets = LocalDirAssetSource(tmp_path / "assets")
    ch = channel()
    client = FakeYouTubeClient()
    service = make_service(store, keys, InMemoryAssetSource(), client, fake_secrets())
    mem = service._assets
    job = store.create_job(ch, "planner", actor="planner")
    chash = drive_to_awaiting(store, mem, job, ch)  # type: ignore[arg-type]
    token = keys.token(job, ch, chash)
    store.verify_approval(job, token, actor="approval-hub")
    store.transition(job, JobState.SCHEDULED, actor="scheduler")
    service.publish(job, token)
    for key in mem.list_keys():  # type: ignore[attr-defined]
        assets.write_raw(key, mem.read(key))
    return database, assets


def test_backup_restore_drill_succeeds_and_verifies_everything(
    populated: tuple[str, LocalDirAssetSource], tmp_path: Path
) -> None:
    database, assets = populated
    out = tmp_path / "backup"
    manifest = backup.backup_postgres(database, out)
    backup_store = LocalDirAssetSource(out / "storage")
    objects = backup.backup_storage(assets, backup_store, out)
    assert objects and manifest["database"]["audit_chain_intact"]

    result = backup.restore_drill(
        database,
        out,
        backup_assets=backup_store,
        scratch_assets=LocalDirAssetSource(tmp_path / "r"),
    )
    assert result.ok, result.problems
    assert result.restored_objects == len(objects) and result.seconds < 120


def test_restored_database_keeps_every_safety_invariant(
    populated: tuple[str, LocalDirAssetSource], tmp_path: Path, sql: SQL
) -> None:
    database, _ = populated
    out = tmp_path / "backup"
    backup.backup_postgres(database, out)
    name = "ytcore_restore_check"
    target = backup.restore_postgres(database, out, name)
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            assert conn.execute("SELECT audit_chain_first_break()").fetchone() == (None,)
            for stmt in (
                "UPDATE audit_events SET action = 'x'",  # append-only still enforced
                "UPDATE job_events SET actor = 'x'",
                "TRUNCATE approvals CASCADE",
            ):
                with pytest.raises(psycopg.Error):
                    conn.execute(stmt)
            # the DB-level publish gate survived the restore: no approval, no scheduling
            ch = conn.execute("SELECT id FROM channels LIMIT 1").fetchone()
            assert ch is not None
            job = conn.execute(
                "INSERT INTO video_jobs (channel_id, origin) VALUES (%s, 'owner') RETURNING id",
                (ch[0],),
            ).fetchone()
            assert job is not None
            for state in ("researching", "scripting", "rendering", "qa"):
                conn.execute("UPDATE video_jobs SET state = %s WHERE id = %s", (state, job[0]))
            with pytest.raises(psycopg.Error):
                conn.execute("UPDATE video_jobs SET state = 'approved' WHERE id = %s", (job[0],))
    finally:
        with psycopg.connect(database, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_a_corrupted_dump_is_refused(
    populated: tuple[str, LocalDirAssetSource], tmp_path: Path
) -> None:
    database, _ = populated
    out = tmp_path / "backup"
    backup.backup_postgres(database, out)
    dump = out / backup.DUMP_NAME
    dump.write_bytes(dump.read_bytes()[:-50] + b"x" * 50)
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.restore_drill(database, out)


def test_verification_catches_missing_rows_and_altered_storage(
    populated: tuple[str, LocalDirAssetSource], tmp_path: Path
) -> None:
    database, assets = populated
    out = tmp_path / "backup"
    backup.backup_postgres(database, out)
    manifest_path = out / backup.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["database"]["row_counts"]["video_jobs"] += 1  # pretend a row was lost
    manifest_path.write_text(json.dumps(manifest))
    result = backup.restore_drill(database, out)
    assert not result.ok and any("video_jobs" in p for p in result.problems)

    backup_store = LocalDirAssetSource(out / "storage")
    backup.backup_storage(assets, backup_store, out)
    target = LocalDirAssetSource(tmp_path / "t")
    backup.restore_storage(backup_store, target, out)
    key = target.list_keys()[0]
    target.write_raw(key, b"tampered")
    assert any("differs" in p for p in backup.verify_storage(target, out))
    target = LocalDirAssetSource(tmp_path / "empty")
    assert any("missing" in p for p in backup.verify_storage(target, out))


def test_backup_refuses_a_database_with_a_broken_audit_chain(
    populated: tuple[str, LocalDirAssetSource], tmp_path: Path
) -> None:
    database, _ = populated
    good = tmp_path / "good"
    backup.backup_postgres(database, good)
    name = "ytcore_tamper_check"
    scratch = backup.restore_postgres(database, good, name)  # tamper with a COPY, not the shared db
    try:
        with psycopg.connect(scratch, autocommit=True) as conn:
            conn.execute("SET session_replication_role = replica")
            conn.execute(
                "UPDATE audit_events SET action = 'tampered' "
                "WHERE id = (SELECT min(id) FROM audit_events)"
            )
        with pytest.raises(backup.BackupError, match="audit chain"):
            backup.backup_postgres(scratch, tmp_path / "bad")
    finally:
        with psycopg.connect(database, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_restore_never_overwrites_an_existing_database(
    populated: tuple[str, LocalDirAssetSource], tmp_path: Path
) -> None:
    database, _ = populated
    out = tmp_path / "backup"
    backup.backup_postgres(database, out)
    with psycopg.connect(database, autocommit=True) as conn:
        existing = conn.execute("SELECT current_database()").fetchone()
    assert existing is not None
    with pytest.raises(backup.BackupError, match="existing"):
        backup.restore_postgres(database, out, existing[0])


def test_local_asset_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = LocalDirAssetSource(tmp_path / "s")
    for key in ("../x", "/etc/passwd", "a/../../x", "a\\b", ""):
        with pytest.raises(Exception, match="invalid asset key"):
            store.write_raw(key, b"x")

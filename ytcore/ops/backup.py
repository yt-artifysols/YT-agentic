"""Backups and a restore drill for Postgres and object storage.

Postgres: `pg_dump -Fc` plus a manifest (dump sha256, applied migrations, row count per table,
audit-chain state at backup time). Storage: a manifest of every object (key, sha256, size) and a
copy into a backup store. A restore drill restores both into scratch targets and VERIFIES them
against the manifests, so "we have backups" means "we restored one and it checked out".

Credentials: connection strings come from the libpq environment or a conninfo handed in by the
caller; passwords are never put on a command line or in a manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from ytcore.approvals.hashing import file_digest
from ytcore.gateway.assets import ListableAssets

DUMP_NAME = "postgres.dump"
MANIFEST_NAME = "backup-manifest.json"
STORAGE_MANIFEST = "storage-manifest.json"
# Triggers/functions that carry safety invariants must exist after a restore.
REQUIRED_FUNCTIONS = (
    "audit_chain_first_break",
    "channel_not_paused",
    "channel_publishable",
    "forbid_mutation",
)


class BackupError(Exception):
    pass


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BackupError(f"{name} not found on PATH")
    return path


def _pg_connection(conninfo: str) -> tuple[list[str], dict[str, str]]:
    """libpq arguments for pg_dump/pg_restore built from a conninfo or URI, with the password
    (and other secrets) in the ENVIRONMENT, never on the command line."""
    parts = psycopg.conninfo.conninfo_to_dict(conninfo)
    args: list[str] = []
    for key, flag in (("host", "-h"), ("port", "-p"), ("user", "-U")):
        if parts.get(key):
            args += [flag, str(parts[key])]
    env: dict[str, str] = {}
    if parts.get("password"):
        env["PGPASSWORD"] = str(parts["password"])
    if parts.get("sslmode"):
        env["PGSSLMODE"] = str(parts["sslmode"])
    return [*args, "-d", str(parts.get("dbname") or "postgres")], env


def _run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    result = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, check=False, env={**os.environ, **(env or {})}
    )
    if result.returncode != 0:
        # stderr from pg tools never contains the password, but keep it short anyway
        raise BackupError(f"{Path(cmd[0]).name} failed: {result.stderr.strip()[:500]}")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ----------------------------------------------------------------------------- fingerprints


@dataclass
class DbFingerprint:
    migrations: dict[str, str]
    row_counts: dict[str, int]
    audit_events: int
    audit_chain_intact: bool
    functions: list[str] = field(default_factory=list)
    triggers: int = 0


def fingerprint(conninfo: str) -> DbFingerprint:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        migrations: dict[str, str] = dict(
            conn.execute("SELECT name, sha256 FROM schema_migrations").fetchall()
        )
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1"
            ).fetchall()
        ]
        counts = {}
        for table in tables:
            row = conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()
            counts[table] = int(row[0]) if row else 0
        chain = conn.execute("SELECT audit_chain_first_break()").fetchone()
        funcs = [
            r[0]
            for r in conn.execute(
                "SELECT proname FROM pg_proc WHERE proname = ANY(%s) ORDER BY 1",
                (list(REQUIRED_FUNCTIONS),),
            ).fetchall()
        ]
        trig = conn.execute("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal").fetchone()
    return DbFingerprint(
        migrations=migrations,
        row_counts=counts,
        audit_events=counts.get("audit_events", 0),
        audit_chain_intact=chain is not None and chain[0] is None,
        functions=funcs,
        triggers=int(trig[0]) if trig else 0,
    )


# ----------------------------------------------------------------------------- postgres


def backup_postgres(conninfo: str, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    before = fingerprint(conninfo)
    if not before.audit_chain_intact:
        raise BackupError("the audit chain is broken: refusing to back up a tampered database")
    dump = out_dir / DUMP_NAME
    conn_args, conn_env = _pg_connection(conninfo)
    _run(
        [_tool("pg_dump"), "-Fc", "--no-owner", "--no-acl", *conn_args, "-f", str(dump)],
        conn_env,
    )
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "dump": DUMP_NAME,
        "dump_sha256": _sha256_file(dump),
        "dump_bytes": dump.stat().st_size,
        "database": asdict(before),
    }
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def restore_postgres(admin_conninfo: str, backup_dir: Path, target_db: str) -> str:
    """Restore into a NEW database `target_db` (never over an existing one); returns its
    conninfo (admin conninfo with the database swapped)."""
    manifest = json.loads((backup_dir / MANIFEST_NAME).read_text())
    dump = backup_dir / manifest["dump"]
    if _sha256_file(dump) != manifest["dump_sha256"]:
        raise BackupError("dump checksum mismatch: the backup is corrupt or was tampered with")
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
        if exists.fetchone() is not None:
            raise BackupError(f"refusing to restore over existing database {target_db!r}")
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db)))
    target = psycopg.conninfo.make_conninfo(admin_conninfo, dbname=target_db)
    _run(
        [_tool("pg_restore"), "--no-owner", "--no-acl", "--exit-on-error", "-d", target, str(dump)]
    )
    return target


def verify_restore(backup_dir: Path, restored_conninfo: str) -> list[str]:
    """Problems found comparing the restored database to the backup manifest (empty = good)."""
    expected = json.loads((backup_dir / MANIFEST_NAME).read_text())["database"]
    got = fingerprint(restored_conninfo)
    problems: list[str] = []
    if got.migrations != expected["migrations"]:
        problems.append("applied migrations differ from the backup")
    for table, n in expected["row_counts"].items():
        if got.row_counts.get(table) != n:
            problems.append(f"row count differs for {table}: {got.row_counts.get(table)} != {n}")
    if not got.audit_chain_intact:
        problems.append("audit hash chain does not verify after restore")
    if sorted(got.functions) != sorted(REQUIRED_FUNCTIONS):
        problems.append(f"safety functions missing: {set(REQUIRED_FUNCTIONS) - set(got.functions)}")
    if got.triggers != expected["triggers"]:
        problems.append(f"trigger count differs: {got.triggers} != {expected['triggers']}")
    with psycopg.connect(restored_conninfo, autocommit=True) as conn:
        if conn.execute(
            "SELECT count(*) FROM pg_extension WHERE extname = 'vector'"
        ).fetchone() == (0,):
            problems.append("pgvector extension missing")
        if conn.execute("SELECT count(*) FROM global_controls").fetchone() != (1,):
            problems.append("global_controls must hold exactly one row")
    return problems


# ----------------------------------------------------------------------------- storage


def storage_manifest(source: ListableAssets, prefix: str = "") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key in source.list_keys(prefix):
        data = source.read(key)
        out[key] = {"sha256": file_digest(data), "bytes": len(data)}
    return out


def backup_storage(
    source: ListableAssets, backup: ListableAssets, out_dir: Path, prefix: str = ""
) -> dict[str, dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = storage_manifest(source, prefix)
    for key in manifest:
        backup.write_raw(key, source.read(key))
    (out_dir / STORAGE_MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def restore_storage(backup: ListableAssets, target: ListableAssets, out_dir: Path) -> int:
    manifest = json.loads((out_dir / STORAGE_MANIFEST).read_text())
    for key, meta in manifest.items():
        data = backup.read(key)
        if file_digest(data) != meta["sha256"]:
            raise BackupError(f"backup object {key} is corrupt")
        target.write_raw(key, data)
    return len(manifest)


def verify_storage(target: ListableAssets, out_dir: Path) -> list[str]:
    manifest = json.loads((out_dir / STORAGE_MANIFEST).read_text())
    problems = []
    present = set(target.list_keys(""))
    for key, meta in manifest.items():
        if key not in present:
            problems.append(f"missing object {key}")
        elif file_digest(target.read(key)) != meta["sha256"]:
            problems.append(f"object {key} differs from the backup")
    return problems


# ----------------------------------------------------------------------------- drill


@dataclass
class DrillResult:
    ok: bool
    problems: list[str]
    seconds: float
    restored_tables: int
    restored_objects: int


def restore_drill(
    admin_conninfo: str,
    backup_dir: Path,
    *,
    backup_assets: ListableAssets | None = None,
    scratch_assets: ListableAssets | None = None,
    keep: bool = False,
) -> DrillResult:
    """Restore the latest backup into a scratch database (and scratch storage), verify it, and
    drop the scratch database. This is the procedure docs/runbooks/backup-restore.md describes."""
    started = datetime.now(UTC)
    name = f"ytcore_drill_{uuid.uuid4().hex[:10]}"
    problems: list[str] = []
    target = restore_postgres(admin_conninfo, backup_dir, name)
    try:
        problems += verify_restore(backup_dir, target)
        objects = 0
        if backup_assets is not None and scratch_assets is not None:
            objects = restore_storage(backup_assets, scratch_assets, backup_dir)
            problems += verify_storage(scratch_assets, backup_dir)
        tables = len(json.loads((backup_dir / MANIFEST_NAME).read_text())["database"]["row_counts"])
    finally:
        if not keep:
            with psycopg.connect(admin_conninfo, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )
    return DrillResult(
        ok=not problems,
        problems=problems,
        seconds=(datetime.now(UTC) - started).total_seconds(),
        restored_tables=tables,
        restored_objects=objects,
    )


def _conninfo_from_secrets() -> str:  # pragma: no cover - needs configured secrets
    from ytcore.config import build_secrets_provider, load_settings
    from ytcore.secrets.provider import SecretRef

    core = load_settings()
    return build_secrets_provider(core).get(SecretRef(core.database_url_ref)).reveal()


def _upload(directory: Path, bucket: str) -> None:  # pragma: no cover - needs a real bucket
    """Copy the backup files under backups/<UTC timestamp>/ in the backups bucket."""
    from ytcore.gateway.assets import GcsAssetSource

    target = GcsAssetSource(bucket)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        target.write_raw(
            f"backups/{stamp}/{path.relative_to(directory).as_posix()}", path.read_bytes()
        )
    print(f"uploaded to gs://{bucket}/backups/{stamp}/")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    parser = argparse.ArgumentParser(prog="python -m ytcore.ops.backup")
    parser.add_argument("cmd", choices=["backup", "restore-drill"])
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--assets-dir", type=Path, help="local asset store to back up / verify")
    parser.add_argument("--bucket", help="also upload the backup to this (backups) bucket")
    args = parser.parse_args(argv)
    conninfo = os.environ.get("BACKUP_CONNINFO") or _conninfo_from_secrets()
    if args.cmd == "backup":
        manifest = backup_postgres(conninfo, args.dir)
        print(f"postgres backup ok: {manifest['dump_bytes']} bytes")
        if args.bucket:
            _upload(args.dir, args.bucket)
        if args.assets_dir:
            from ytcore.gateway.assets import LocalDirAssetSource

            src = LocalDirAssetSource(args.assets_dir)
            copy = LocalDirAssetSource(args.dir / "storage")
            n = len(backup_storage(src, copy, args.dir))
            print(f"storage backup ok: {n} objects")
        return 0
    from ytcore.gateway.assets import LocalDirAssetSource

    has_storage = (args.dir / STORAGE_MANIFEST).exists()
    admin = psycopg.conninfo.make_conninfo(conninfo, dbname="postgres")
    drill = restore_drill(
        admin,
        args.dir,
        backup_assets=LocalDirAssetSource(args.dir / "storage") if has_storage else None,
        scratch_assets=LocalDirAssetSource(args.dir / "restored") if has_storage else None,
    )
    print(json.dumps(asdict(drill), indent=2))
    return 0 if drill.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

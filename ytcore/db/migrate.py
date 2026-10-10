"""Apply numbered SQL migrations with psql, recording a checksum per file.

A migration whose checksum differs from the recorded one aborts the run: applied migrations
are immutable. The exception is a migration whose first line is `-- repeatable` (the generated
transitions seed), which is idempotent and re-applied when it changes.

The connection comes from the standard libpq environment (PGHOST, PGUSER, PGDATABASE, ...) or
DATABASE_URL; no credentials are accepted on the command line.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_NAME = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    files = sorted(p for p in directory.glob("*.sql"))
    bad = [p.name for p in files if not _NAME.match(p.name)]
    if bad:
        raise SystemExit(f"badly named migrations: {bad}")
    numbers = [p.name[:3] for p in files]
    if len(set(numbers)) != len(numbers):
        raise SystemExit("duplicate migration numbers")
    return files


REPEATABLE_MARKER = b"-- repeatable\n"


def is_repeatable(path: Path) -> bool:
    """Repeatable migrations (generated seeds) are re-applied when their checksum changes."""
    return path.read_bytes().startswith(REPEATABLE_MARKER)


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _psql(*args: str, stdin: str | None = None) -> str:
    psql = shutil.which("psql")
    if psql is None:
        raise SystemExit("psql not found on PATH")
    cmd = [psql, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-At", *args]
    url = os.environ.get("DATABASE_URL")
    if url:
        cmd += ["-d", url]
    result = subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip())
    return result.stdout


def main() -> int:
    _psql(
        "-c",
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name text PRIMARY KEY, sha256 text NOT NULL,"
        " applied_at timestamptz NOT NULL DEFAULT now())",
    )
    applied = dict(
        line.split("|", 1)
        for line in _psql("-c", "SELECT name, sha256 FROM schema_migrations").splitlines()
        if line
    )
    for path in migration_files():
        digest = checksum(path)
        if path.name in applied:
            if applied[path.name] == digest:
                continue
            if not is_repeatable(path):
                print(f"applied migration {path.name} was modified", file=sys.stderr)
                return 1
            _psql("-f", str(path))
            _psql(
                "-c",
                # Safe: name is validated by _NAME and digest is hex.
                f"UPDATE schema_migrations SET sha256 = '{digest}', applied_at = now() "  # noqa: S608
                f"WHERE name = '{path.name}'",
            )
            print(f"re-applied repeatable {path.name}")
            continue
        _psql("-f", str(path))
        _psql(
            "-c",
            # Safe: name is validated by _NAME and digest is hex.
            f"INSERT INTO schema_migrations (name, sha256) VALUES ('{path.name}', '{digest}')",  # noqa: S608
        )
        print(f"applied {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

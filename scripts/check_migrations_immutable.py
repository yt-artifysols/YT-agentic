"""Fail if an applied migration (one that exists on the base branch) was modified or deleted.

Usage: python scripts/check_migrations_immutable.py [--base origin/main]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

MIGRATIONS = "ytcore/db/migrations"
REPEATABLE = b"-- repeatable\n"
ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)  # noqa: S603, S607


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root: Path = args.root

    if _git(root, "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}").returncode != 0:
        print(f"base ref {args.base} not found; skipping migration immutability check")
        return 0

    listing = _git(root, "ls-tree", "-r", "--name-only", args.base, "--", MIGRATIONS)
    if listing.returncode != 0:
        print(listing.stderr.decode(), file=sys.stderr)
        return 1

    problems: list[str] = []
    for name in listing.stdout.decode().split():
        if not name.endswith(".sql"):
            continue
        base_blob = _git(root, "show", f"{args.base}:{name}").stdout
        local = root / name
        if not local.exists():
            problems.append(f"deleted: {name}")
        elif local.read_bytes() != base_blob:
            # Generated repeatable migrations may change, but only if they were repeatable on
            # the base branch too (the marker cannot be added later to unlock an old file).
            local_bytes = local.read_bytes()
            if base_blob.startswith(REPEATABLE) and local_bytes.startswith(REPEATABLE):
                continue
            problems.append(f"modified: {name}")
    if problems:
        print("Applied migrations are immutable; add a new numbered migration instead:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"applied migrations unchanged relative to {args.base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

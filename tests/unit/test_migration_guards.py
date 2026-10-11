from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_migrations_immutable", REPO / "scripts" / "check_migrations_immutable.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    mig = tmp_path / "ytcore" / "db" / "migrations"
    mig.mkdir(parents=True)
    (mig / "001_init.sql").write_text("CREATE TABLE a ();\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_unchanged_and_new_migrations_pass(repo: Path) -> None:
    (repo / "ytcore/db/migrations/002_more.sql").write_text("CREATE TABLE b ();\n")
    assert _load_checker().main(["--base", "main", "--root", str(repo)]) == 0


def test_modified_applied_migration_fails(repo: Path) -> None:
    (repo / "ytcore/db/migrations/001_init.sql").write_text("CREATE TABLE a (x int);\n")
    assert _load_checker().main(["--base", "main", "--root", str(repo)]) == 1


def test_deleted_applied_migration_fails(repo: Path) -> None:
    (repo / "ytcore/db/migrations/001_init.sql").unlink()
    assert _load_checker().main(["--base", "main", "--root", str(repo)]) == 1


def test_repeatable_migration_may_change(repo: Path) -> None:
    path = repo / "ytcore/db/migrations/002_seed.sql"
    path.write_text("-- repeatable\nSELECT 1;\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    path.write_text("-- repeatable\nSELECT 2;\n")
    assert _load_checker().main(["--base", "main", "--root", str(repo)]) == 0


def test_marker_cannot_unlock_an_applied_migration(repo: Path) -> None:
    (repo / "ytcore/db/migrations/001_init.sql").write_text("-- repeatable\nDROP TABLE a;\n")
    assert _load_checker().main(["--base", "main", "--root", str(repo)]) == 1


def test_missing_base_is_skipped(repo: Path) -> None:
    assert _load_checker().main(["--base", "origin/nope", "--root", str(repo)]) == 0


def test_migration_files_are_numbered_and_unique() -> None:
    from ytcore.db.migrate import migration_files

    names = [p.name for p in migration_files()]
    assert names == sorted(names)
    assert names[:2] == ["001_init.sql", "002_transitions_seed.sql"]


def test_only_the_generated_seed_is_repeatable() -> None:
    from ytcore.db.migrate import is_repeatable, migration_files

    assert [p.name for p in migration_files() if is_repeatable(p)] == ["002_transitions_seed.sql"]

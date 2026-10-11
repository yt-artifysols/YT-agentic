"""The runbooks the pilot depends on exist, link to each other, and name real commands."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS = ROOT / "docs" / "runbooks"
REQUIRED = {
    "key-rotation.md",
    "kill-switch.md",
    "stuck-job-recovery.md",
    "quota-exhaustion.md",
    "strike-response.md",
    "channel-suspension-restore.md",
    "backup-restore.md",
}


def test_required_runbooks_exist_and_are_indexed() -> None:
    index = (RUNBOOKS / "README.md").read_text(encoding="utf-8")
    for name in REQUIRED:
        assert (RUNBOOKS / name).is_file(), name
        assert name in index, f"{name} is missing from the runbook index"


def test_relative_links_resolve() -> None:
    for path in RUNBOOKS.glob("*.md"):
        for target in re.findall(
            r"\]\(([^)#]+\.md)(?:#[^)]*)?\)", path.read_text(encoding="utf-8")
        ):
            assert (RUNBOOKS / target).is_file(), f"{path.name} links to missing {target}"


def test_commands_named_in_runbooks_exist() -> None:
    from ytcore.cli import _parser

    args = {
        "stuck": [],
        "pending-uploads": [],
        "plans": [],
        "adopt-upload": ["--upload", "u", "--video-id", "v"],
        "release-upload": ["--upload", "u", "--reason", "r"],
    }
    text = "\n".join(p.read_text(encoding="utf-8") for p in RUNBOOKS.glob("*.md"))
    for cmd in set(re.findall(r"ytctl ops ([a-z-]+)", text)):
        assert cmd in args, f"runbook names unknown command: ytctl ops {cmd}"
        _parser().parse_args(["ops", cmd, *args[cmd]])
    for module in set(re.findall(r"python -m (ytcore[\w.]+)", text)):
        base = ROOT / module.replace(".", "/")
        assert base.with_suffix(".py").is_file() or (base / "__main__.py").is_file(), module

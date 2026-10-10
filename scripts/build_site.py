"""Build the static development-status site (site/index.html) from the repository.

Everything shown is derived from the code: job states and transitions from states.py,
migrations, test inventory (by AST), ADRs, the threat model and git history. No secrets or
environment data are read. Deployed as a static page (e.g. Vercel).

Usage: python scripts/build_site.py
"""

from __future__ import annotations

import ast
import html
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ytcore.workflow.states import (  # noqa: E402
    APPROVAL_GATED_STATES,
    TERMINAL_STATES,
    JobState,
    sorted_transitions,
)

OUT = ROOT / "site" / "index.html"

PHASES = [
    (
        "Phase 0",
        "Foundations and repo hygiene",
        "done",
        "Core state machine, approval tokens, schema with DB-enforced gate, CI, pre-commit, "
        "secrets provider, typed settings, ADRs, threat model, Terraform skeleton.",
    ),
    (
        "Phase 1A",
        "Postgres store and YouTube gateway",
        "done",
        "psycopg store with typed trigger errors, single publish path through the gateway, "
        "quota ledger, uploads with idempotency, budget enforcement, full audit trail.",
    ),
    (
        "Phase 1B",
        "Approval hub (API and UI)",
        "done",
        "FastAPI hub issuing Ed25519 tokens via a KeyProvider, passkey sign-in, roles, CSRF, "
        "rate limits; approve/edit/reject/fast-lane batch; kill switches; Next.js UI with "
        "risk-sorted queue, preview, flags, sources and diff view.",
    ),
]

INVARIANTS = [
    "No publish without a valid Ed25519 approval token bound to job, channel and content hash.",
    "Only the YouTube gateway talks to the upload API; it holds only the public key.",
    "states.py is the single source of truth for job states.",
    "Applied migrations are immutable; job_events and audit_events are append-only.",
    "Secrets only via vault references through a SecretsProvider.",
    "Scraped or external content is untrusted data, never instructions.",
    "Per-channel isolation: memory, storage, policy, OAuth token, budget.",
    "Nothing is auto-published; a missed approval deadline cancels and re-plans.",
]


def git(*args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def commits(limit: int = 15) -> list[dict[str, str]]:
    raw = git("log", f"-{limit}", "--no-merges", "--format=%h%x1f%ad%x1f%s", "--date=short")
    out = []
    for line in raw.splitlines():
        sha, date, subject = line.split("\x1f", 2)
        out.append({"sha": sha, "date": date, "subject": subject})
    return out


def tests() -> list[dict[str, object]]:
    groups = []
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        tree = ast.parse(path.read_text())
        names = [
            n.name
            for n in tree.body
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test_")
        ]
        groups.append({"file": str(path.relative_to(ROOT)), "tests": names})
    call = re.compile(
        r"SELECT pg_temp\.(?:expect_error|expect_ok|check)\((.*?)\);\s*$", re.S | re.M
    )
    notice = re.compile(r"RAISE NOTICE 'ok   ([^'%]+)'")
    for path in sorted((ROOT / "tests").glob("schema_guard*_test.sql")):
        text = path.read_text()
        labels = [re.findall(r"'([^']*)'", m.group(1))[-1] for m in call.finditer(text)]
        labels += notice.findall(text)
        groups.append({"file": str(path.relative_to(ROOT)), "tests": labels})
    return groups


def mermaid() -> str:
    lines = ["stateDiagram-v2", "    [*] --> planned"]
    for src, dst in sorted_transitions():
        if dst in {JobState.CANCELLED, JobState.FAILED} and src not in {
            JobState.AWAITING_APPROVAL,
            JobState.PUBLISHING,
        }:
            continue  # keep the diagram readable; abort edges are listed in the table
        lines.append(f"    {src.value} --> {dst.value}")
    for state in sorted(TERMINAL_STATES, key=lambda s: s.value):
        lines.append(f"    {state.value} --> [*]")
    return "\n".join(lines)


def docs() -> list[dict[str, str]]:
    items = [
        {"title": p.stem, "markdown": p.read_text()}
        for p in sorted((ROOT / "docs" / "adr").glob("*.md"))
    ]
    items.append({"title": "threat-model", "markdown": (ROOT / "docs/threat-model.md").read_text()})
    return items


def build() -> str:
    head = git("rev-parse", "--short", "HEAD") or "unknown"
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    data = {
        "built": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "head": head,
        "branch": branch,
        "phases": [{"id": a, "title": b, "status": c, "summary": d} for a, b, c, d in PHASES],
        "invariants": INVARIANTS,
        "states": [
            {
                "state": s.value,
                "terminal": s in TERMINAL_STATES,
                "gated": s in APPROVAL_GATED_STATES,
                "next": [d.value for src, d in sorted_transitions() if src == s],
            }
            for s in JobState
        ],
        "mermaid": mermaid(),
        "migrations": [p.name for p in sorted((ROOT / "ytcore/db/migrations").glob("*.sql"))],
        "tests": tests(),
        "commits": commits(),
        "docs": docs(),
    }
    payload = json.dumps(data).replace("</", "<\\/")
    template = (ROOT / "scripts" / "site_template.html").read_text()
    return template.replace("/*__DATA__*/null", payload).replace(
        "__TITLE__", html.escape("yt-agentic dev status")
    )


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(build())
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

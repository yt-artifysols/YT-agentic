"""Check the database after the UI e2e run: tokens, approvals rows, outcomes, audit events."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

stack = json.loads(Path(__file__).with_name(".stack.json").read_text())
jobs = stack["jobs"]
failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


with psycopg.connect(f"dbname={stack['database']}", autocommit=True) as conn:

    def q(sql: str, *params: Any) -> list[tuple[Any, ...]]:
        return conn.execute(sql, params).fetchall()  # type: ignore[arg-type]

    for _ in range(100):  # let the workflows finish publishing
        states = dict(q("SELECT id::text, state FROM video_jobs"))
        if states[jobs["approve"]] == "live" and states[jobs["edit"]] == "live":
            break
        time.sleep(0.2)
    states = dict(q("SELECT id::text, state FROM video_jobs"))
    check(states[jobs["approve"]] == "live", "approved job went live")
    check(states[jobs["edit"]] == "live", "edited job went live after re-approval")
    check(states[jobs["reject"]] == "rejected", "rejected (discard) job is rejected")

    for label in ("approve", "edit"):
        rows = q(
            "SELECT a.approver, a.content_hash = j.content_hash, u.approval_jti = a.token_jti "
            "FROM approvals a JOIN video_jobs j ON j.id = a.job_id "
            "JOIN uploads u ON u.job_id = a.job_id WHERE a.job_id = %s",
            jobs[label],
        )
        check(
            rows == [("e2e-approver", True, True)],
            f"{label}: one approval by the approver, for the published content, used by the upload",
        )
    versions = q("SELECT count(*) FROM manifest_versions WHERE job_id = %s", jobs["edit"])[0][0]
    check(versions == 2, "edit created a second manifest version")
    title = q("SELECT manifest->'metadata'->>'title' FROM video_jobs WHERE id = %s", jobs["edit"])
    check(title == [("Edited in the hub",)], "published manifest carries the edited title")
    rejected = q("SELECT count(*) FROM approvals WHERE job_id = %s", jobs["reject"])[0][0]
    check(rejected == 0, "rejected job has no approval")

    def audit(action: str, subject: str) -> int:
        rows = q(
            "SELECT count(*) FROM audit_events WHERE action = %s AND subject = %s", action, subject
        )
        return int(rows[0][0])

    check(audit("hub.approve", f"job:{jobs['approve']}") == 1, "approve audited")
    check(audit("hub.approve", f"job:{jobs['edit']}") == 1, "re-approval audited")
    check(audit("hub.edit", f"job:{jobs['edit']}") == 1, "edit audited")
    check(audit("approval.rejected", f"job:{jobs['reject']}") == 1, "reject audited")
    check(audit("hub.login", "user:e2e-approver") == 2, "approver passkey logins audited")
    check(audit("killswitch.engaged", f"channel:{stack['channel']}") == 1, "kill switch audited")
    check(audit("killswitch.released", f"channel:{stack['channel']}") == 1, "release audited")
    check(q("SELECT audit_chain_first_break()")[0][0] is None, "audit hash chain intact")

sys.exit(1 if failures else 0)

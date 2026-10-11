"""ytctl: operator command line.

    ytctl input add --channel CHAN --kind idea --text "Why X beats Y"
    ytctl input add --channel CHAN --kind footage --file clip.mp4      (sha256 computed locally)
    ytctl input add --channel CHAN --kind footage --uri gs://b/clip.mp4 --sha256 sha256:...
    ytctl input list --channel CHAN [--status new]
    ytctl knowledge show --channel CHAN
    ytctl knowledge propose --channel CHAN --kind banned_claims --file claims.md [--base-version N]

    ytctl ops stuck | pending-uploads | plans
    ytctl ops adopt-upload --upload ID --video-id VIDEO      (after checking YouTube Studio)
    ytctl ops release-upload --upload ID --reason "no video exists on YouTube, checked ..."

Knowledge proposals are drafts; only a human approver can approve them in the approval hub.
The database DSN comes from the configured SecretsProvider (YT_DATABASE_URL_REF).
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from ytcore.approvals.hashing import file_digest
from ytcore.inputs.models import InputKind
from ytcore.inputs.store import InputStore, PostgresInputs
from ytcore.knowledge.models import KnowledgeKind
from ytcore.knowledge.store import PostgresKnowledge
from ytcore.safety.scrub import install_log_scrubber
from ytcore.store.errors import StoreError


def _conninfo() -> str:
    from ytcore.config import build_secrets_provider, load_settings
    from ytcore.secrets.provider import SecretRef

    core = load_settings()
    return build_secrets_provider(core).get(SecretRef(core.database_url_ref)).reveal()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ytctl")
    sub = parser.add_subparsers(dest="area", required=True)

    inp = sub.add_parser("input", help="owner inputs").add_subparsers(dest="cmd", required=True)
    add = inp.add_parser("add")
    add.add_argument("--channel", required=True)
    add.add_argument("--kind", choices=[k.value for k in InputKind], required=True)
    src = add.add_mutually_exclusive_group(required=True)
    src.add_argument("--text")
    src.add_argument("--file", type=Path)
    src.add_argument("--uri")
    add.add_argument("--sha256", help="sha256 of the content behind --uri")
    add.add_argument("--note", help="optional text that goes with --file/--uri")
    lst = inp.add_parser("list")
    lst.add_argument("--channel", required=True)
    lst.add_argument("--status", choices=["new", "used", "archived"])

    kb = sub.add_parser("knowledge", help="knowledge base").add_subparsers(
        dest="cmd", required=True
    )
    show = kb.add_parser("show")
    show.add_argument("--channel", required=True)
    prop = kb.add_parser("propose")
    prop.add_argument("--channel", required=True)
    prop.add_argument("--kind", choices=[k.value for k in KnowledgeKind], required=True)
    prop.add_argument("--file", type=Path, required=True)
    prop.add_argument("--base-version", type=int)

    ops = sub.add_parser("ops", help="operations (see docs/runbooks)").add_subparsers(
        dest="cmd", required=True
    )
    ops.add_parser("stuck", help="jobs, budgets, quota and uploads that need attention")
    ops.add_parser("pending-uploads", help="uploads left pending by a crash")
    adopt = ops.add_parser("adopt-upload", help="the video exists on YouTube: record it")
    adopt.add_argument("--upload", required=True)
    adopt.add_argument("--video-id", required=True)
    release = ops.add_parser("release-upload", help="no video exists: fail the job to re-plan it")
    release.add_argument("--upload", required=True)
    release.add_argument("--reason", required=True)
    plans = ops.add_parser("plans", help="upcoming daily plans")
    plans.add_argument("--limit", type=int, default=30)
    return parser


def _ops(args: argparse.Namespace, who: str) -> int:
    import psycopg

    from ytcore.observability.monitor import OpsMonitor
    from ytcore.ops import reconcile
    from ytcore.store.postgres import PostgresStore

    dsn = _conninfo()

    def connect() -> psycopg.Connection[Any]:
        return psycopg.connect(dsn, autocommit=True)

    operator = who.removeprefix("cli:")
    if args.cmd == "stuck":
        findings = OpsMonitor(connect).findings()
        for f in findings:
            print(f"{f.kind:<16} {f.subject}: {f.message}")
        print(f"{len(findings)} finding(s)")
        return 1 if findings else 0
    if args.cmd == "pending-uploads":
        for u in reconcile.pending_uploads(connect):
            print(f"{u.upload_id}  job {u.job_id}  channel {u.channel_id}  since {u.created_at}")
        return 0
    if args.cmd == "plans":
        with connect() as conn:
            rows = conn.execute(
                "SELECT channel_id, slot_at, status, attempt, job_id::text FROM daily_plans "
                "ORDER BY slot_at DESC LIMIT %s",
                (args.limit,),
            ).fetchall()
        for r in rows:
            print(f"{r[0]:<24} {r[1]:%Y-%m-%d %H:%M}Z  {r[2]:<16} attempt {r[3]}  job {r[4]}")
        return 0
    # Adopting or releasing writes as the operator; no approval key is involved.
    store = PostgresStore.from_conninfo(dsn, None)
    if args.cmd == "adopt-upload":
        reconcile.adopt_upload(store, args.upload, args.video_id, operator=operator)
        print("upload adopted; the job is published")
    else:
        reconcile.release_upload(store, args.upload, args.reason, operator=operator)
        print("upload released; the job failed and can be re-planned")
    return 0


def main(
    argv: list[str] | None = None,
    *,
    inputs: InputStore | None = None,
    knowledge: Any = None,
    actor: str | None = None,
) -> int:
    install_log_scrubber()
    args = _parser().parse_args(argv)
    who = actor or f"cli:{getpass.getuser()}"
    try:
        if args.area == "input":
            store = inputs or PostgresInputs.from_conninfo(_conninfo())
            if args.cmd == "add":
                body, uri, digest = args.text, args.uri, args.sha256
                if args.file is not None:
                    path = args.file.resolve()
                    uri, digest = path.as_uri(), file_digest(path.read_bytes())
                if uri is not None and args.note:
                    body = args.note
                item = store.add(
                    args.channel,
                    InputKind(args.kind),
                    body=body,
                    uri=uri,
                    sha256=digest,
                    submitted_by=who,
                )
                print(json.dumps({"id": item.id, "kind": item.kind, "sha256": item.sha256}))
                return 0
            for item in store.list_for(args.channel, args.status):
                text = item.uri or item.body or ""
                print(f"{item.id}  {item.kind:<7} {item.status:<8} {text}"[:160])
            return 0

        if args.area == "ops":
            return _ops(args, who)

        base = knowledge or PostgresKnowledge.from_conninfo(_conninfo())
        chan = base.channel(args.channel)
        if args.cmd == "show":
            snap = chan.latest_approved()
            print(json.dumps({"channel": args.channel, "approved_versions": snap.versions()}))
            for draft in chan.drafts():
                print(f"draft {draft.id}  {draft.kind} v{draft.version} by {draft.proposed_by}")
            return 0
        doc = chan.propose(
            KnowledgeKind(args.kind),
            args.file.read_text(encoding="utf-8"),
            proposed_by=who,
            base_version=args.base_version,
        )
        print(f"draft {doc.id}: {doc.kind} v{doc.version}; a human approver must approve it")
        return 0
    except (StoreError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

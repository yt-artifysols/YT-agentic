"""Approval hub admin CLI.

    python -m ytcore.approvals.service.cli gen-dev-key [--path .local/approval_ed25519.pem]
    python -m ytcore.approvals.service.cli invite --username alice --role admin --name "Alice"
    python -m ytcore.approvals.service.cli notify
    python -m ytcore.approvals.service.cli export-openapi [--out path]

`invite` prints a one-time invite token for the new user to register a passkey with. Hand it
over out of band; it expires (YT_HUB_INVITE_TTL_HOURS) and can be used once.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ytcore.approvals.keys import LocalFileKeyProvider
from ytcore.approvals.service.repo import HubRepository
from ytcore.approvals.service.settings import HubSettings

OPENAPI_PATH = Path(__file__).with_name("openapi.json")


def _repo_and_settings() -> tuple[HubRepository, HubSettings]:
    from ytcore.config import build_secrets_provider, load_settings
    from ytcore.secrets.provider import SecretRef

    core = load_settings()
    dsn = build_secrets_provider(core).get(SecretRef(core.database_url_ref)).reveal()
    hub = HubSettings()
    return HubRepository.from_conninfo(dsn, fast_lane_max_risk=hub.fast_lane_max_risk), hub


def openapi_document() -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from ytcore.approvals.service.app import export_openapi
    from ytcore.approvals.service.main import create_hub_app
    from ytcore.approvals.service.notify import FakeNotifier

    app = create_hub_app(
        HubSettings(),
        conninfo="dbname=unused",
        key_provider=Ed25519PrivateKey.generate(),
        notifier=FakeNotifier(),
    )
    return export_openapi(app)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub")
    sub = parser.add_subparsers(dest="cmd", required=True)
    key = sub.add_parser("gen-dev-key")
    key.add_argument("--path", type=Path, default=Path(".local/approval_ed25519.pem"))
    inv = sub.add_parser("invite")
    inv.add_argument("--username", required=True)
    inv.add_argument("--name", required=True)
    inv.add_argument("--role", choices=["approver", "operator", "admin"], required=True)
    sub.add_parser("notify")
    exp = sub.add_parser("export-openapi")
    exp.add_argument("--out", type=Path, default=OPENAPI_PATH)
    exp.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "gen-dev-key":
        if os.environ.get("ENV", "").strip().lower() == "production":
            print("refusing to create a local key file in production; use KMS", file=sys.stderr)
            return 1
        args.path.parent.mkdir(parents=True, exist_ok=True)
        LocalFileKeyProvider.generate(args.path)
        print(f"wrote dev approval key to {args.path} (git-ignored; never commit it)")
        return 0
    if args.cmd == "export-openapi":
        doc = openapi_document()
        if args.check:
            if not args.out.exists() or args.out.read_text() != doc:
                print(f"{args.out} is stale; run export-openapi", file=sys.stderr)
                return 1
            print("openapi spec up to date")
            return 0
        args.out.write_text(doc)
        print(f"wrote {args.out}")
        return 0

    repo, hub_settings = _repo_and_settings()
    if args.cmd == "invite":
        user = repo.create_user(args.username, args.name, args.role, actor="hub:cli")
        token = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(hours=hub_settings.invite_ttl_hours)
        repo.create_invite(user.id, token, expires, actor="hub:cli")
        print(
            f"invite for {user.username} ({user.role}), valid until {expires:%Y-%m-%d %H:%M} UTC:"
        )
        print(token)
        return 0
    if args.cmd == "notify":
        from ytcore.approvals.service.notify import build_notifier, notify_pending
        from ytcore.config import build_secrets_provider, load_settings

        notifier = build_notifier(hub_settings, build_secrets_provider(load_settings()))
        sent = notify_pending(repo, notifier, now=datetime.now(UTC), link=hub_settings.origin)
        print(f"sent {len(sent)} notification(s)")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

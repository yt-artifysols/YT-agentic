"""The Vercel bootstrap builds limited-login URLs without leaking the owner credentials."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "deploy/vercel/bootstrap.py")
assert spec and spec.loader
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)

OWNER = (
    "postgresql://neondb_owner:ownerpw@ep-cool-123.us-east-2.aws.neon.tech/neondb?sslmode=require"
)


def test_service_url_swaps_credentials_and_uses_the_pooler() -> None:
    url = bootstrap.service_url(OWNER, "hub_login", "p/w+1", pooled=True)
    assert url == (
        "postgresql://hub_login:p%2Fw%2B1@ep-cool-123-pooler.us-east-2.aws.neon.tech/neondb"
        "?sslmode=require"
    )
    assert "ownerpw" not in url and "neondb_owner" not in url


def test_service_url_does_not_double_the_pooler_suffix() -> None:
    owner = OWNER.replace("ep-cool-123", "ep-cool-123-pooler")
    assert "-pooler-pooler" not in bootstrap.service_url(owner, "x", "y", pooled=True)


def test_every_login_maps_to_exactly_one_service_role() -> None:
    assert bootstrap.LOGINS == {
        "hub_login": "yt_hub",
        "jobs_login": "yt_worker",
        "ops_login": "yt_ops",
    }

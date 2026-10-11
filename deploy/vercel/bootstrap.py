"""One-shot staging bootstrap for the Vercel deployment (run once; safe to re-run).

Inputs (environment variables; none of them is ever printed or written to disk):
  OWNER_DATABASE_URL_UNPOOLED  the Neon owner login, unpooled (Vercel: DATABASE_URL_UNPOOLED)
  VERCEL_API_TOKEN             a Vercel token with access to the team's projects
Optional: VERCEL_TEAM_ID, HUB_PROJECT, JOBS_PROJECT (defaults below), ADMIN_USERNAME.

What it does:
  1. applies the migrations as the owner
  2. creates three limited logins (hub / jobs / ops), each a member of exactly one service role,
     with fresh random passwords (re-running rotates them)
  3. pushes the service connection strings to the hub and jobs Vercel projects as Sensitive
     variables and removes the owner connection variables the Neon integration put there
  4. applies the example fleet (channels, clusters, draft brand/policy), registers the fake
     providers' licences and prints ONE admin invite code (needed once, to register a passkey)
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
TEAM = os.environ.get("VERCEL_TEAM_ID", "team_VVzkaK4FMuGP5IfwRGfIzxYH")
HUB = os.environ.get("HUB_PROJECT", "yt-agentic-hub-api")
JOBS = os.environ.get("JOBS_PROJECT", "yt-agentic-jobs")
LOGINS = {"hub_login": "yt_hub", "jobs_login": "yt_worker", "ops_login": "yt_ops"}
OWNER_VARS = (
    "DATABASE_URL", "DATABASE_URL_UNPOOLED", "POSTGRES_URL", "POSTGRES_URL_NON_POOLING",
    "POSTGRES_URL_NO_SSL", "POSTGRES_PRISMA_URL", "POSTGRES_USER", "POSTGRES_PASSWORD",
    "POSTGRES_HOST", "POSTGRES_DATABASE", "PGUSER", "PGPASSWORD", "PGHOST", "PGHOST_UNPOOLED",
    "PGDATABASE", "NEON_PROJECT_ID", "NEON_AUTH_BASE_URL", "VITE_NEON_AUTH_URL",
)  # fmt: skip


def need(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"set {name} (store it as an environment secret; never paste it in chat)")
    return value


def service_url(owner_url: str, login: str, password: str, *, pooled: bool) -> str:
    parts = urllib.parse.urlsplit(owner_url)
    host = parts.hostname or ""
    if pooled and "-pooler" not in host:
        first, _, rest = host.partition(".")
        host = f"{first}-pooler.{rest}"
    netloc = f"{login}:{urllib.parse.quote(password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def vercel(method: str, path: str, token: str, body: object | None = None) -> dict[str, object]:
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(
        f"https://api.vercel.com{path}{sep}teamId={TEAM}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:  # noqa: S310
            return json.loads(res.read() or b"{}")  # type: ignore[no-any-return]
    except urllib.error.HTTPError as exc:
        sys.exit(f"Vercel API {method} {path.split('?')[0]} failed: {exc.code}")


def set_env(project: str, token: str, key: str, value: str, kind: str = "sensitive") -> None:
    vercel(
        "POST",
        f"/v10/projects/{project}/env?upsert=true",
        token,
        {"key": key, "value": value, "type": kind, "target": ["production", "preview"]},
    )


def drop_owner_vars(project: str, token: str) -> list[str]:
    removed = []
    envs = vercel("GET", f"/v9/projects/{project}/env", token).get("envs", [])
    for env in envs:  # type: ignore[attr-defined]
        if env["key"] in OWNER_VARS:
            vercel("DELETE", f"/v9/projects/{project}/env/{env['id']}", token)
            removed.append(env["key"])
    return removed


def main() -> int:
    owner = need("OWNER_DATABASE_URL_UNPOOLED")
    token = need("VERCEL_API_TOKEN")
    child_env = {**os.environ, "DATABASE_URL": owner, "ENV": "staging"}
    child_env["YT_SECRETS_BACKEND"] = "platform_env"

    print("1/4 applying migrations")
    subprocess.run([sys.executable, "-m", "ytcore.db.migrate"], cwd=ROOT, env=child_env, check=True)

    print("2/4 creating limited logins")
    passwords = {login: secrets.token_urlsafe(32) for login in LOGINS}
    with psycopg.connect(owner, autocommit=True) as conn:
        for login, role in LOGINS.items():
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (login,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            conn.execute(
                sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(login), sql.Literal(passwords[login])
                )
            )
            conn.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(role), sql.Identifier(login))
            )

    print("3/4 configuring Vercel projects")
    urls = {login: service_url(owner, login, passwords[login], pooled=True) for login in LOGINS}
    set_env(HUB, token, "YT_HUB_DATABASE_URL", urls["hub_login"])
    set_env(JOBS, token, "YT_JOBS_DATABASE_URL", urls["jobs_login"])
    set_env(JOBS, token, "YT_OPS_DATABASE_URL", urls["ops_login"])
    set_env(JOBS, token, "YT_DATABASE_URL_REF", "env://YT_JOBS_DATABASE_URL", "plain")
    for project in (HUB, JOBS):
        removed = drop_owner_vars(project, token)
        print(f"   {project}: removed {len(removed)} owner connection variable(s)")

    print("4/4 fleet, licences, admin invite")
    ytctl = [sys.executable, "-m", "ytcore.cli"]
    child_env["YT_DATABASE_URL_REF"] = "env://DATABASE_URL"
    for args in (
        ["channel", "apply", "--config-dir", "config/examples"],
        ["ops", "seed-staging"],
    ):
        subprocess.run([*ytctl, *args], cwd=ROOT, env=child_env, check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "ytcore.approvals.service.cli",
            "invite",
            "--username",
            os.environ.get("ADMIN_USERNAME", "owner"),
            "--name",
            "Owner",
            "--role",
            "admin",
        ],
        cwd=ROOT,
        env=child_env,
        check=True,
    )
    print("done: redeploy the hub-api and jobs projects so they pick up the new variables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

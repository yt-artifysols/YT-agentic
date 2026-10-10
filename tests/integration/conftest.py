"""Integration fixtures: a throwaway Postgres database with all migrations applied.

Connection comes from the standard PG* environment variables (as in `make test-db`). Tests are
skipped when no server is reachable, unless REQUIRE_DB=1 (CI), in which case they fail.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest

from tests.fakes import fake_secrets
from tests.harness import Keys
from ytcore.store.postgres import PostgresStore

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _admin_conninfo() -> str:
    return f"dbname={os.environ.get('PGDATABASE', 'postgres')}"


@pytest.fixture(scope="session")
def database() -> Iterator[str]:
    try:
        admin = psycopg.connect(_admin_conninfo(), autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("REQUIRE_DB") == "1":
            raise
        pytest.skip(f"no Postgres available: {type(exc).__name__}")
    name = f"ytcore_it_{uuid.uuid4().hex[:10]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    try:
        env = {**os.environ, "PGDATABASE": name}
        env.pop("DATABASE_URL", None)
        subprocess.run(
            [sys.executable, "-m", "ytcore.db.migrate"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
        )
        yield f"dbname={name}"
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def sql(database: str) -> Iterator[Callable[..., list[tuple[Any, ...]]]]:
    conn = psycopg.connect(database, autocommit=True)

    def run(query: str, *params: Any) -> list[tuple[Any, ...]]:
        cur = conn.execute(query, params or None)
        return cur.fetchall() if cur.description else []

    yield run
    conn.close()


@pytest.fixture
def keys() -> Keys:
    return Keys()


@pytest.fixture
def store(database: str, keys: Keys) -> PostgresStore:
    return PostgresStore.from_conninfo(database, keys.verifier)


@pytest.fixture
def channel(sql: Callable[..., list[tuple[Any, ...]]]) -> Callable[..., str]:
    """Create an isolated cluster + Google project + gated channel; returns the channel id."""

    def make(*, quota: int = 100_000, budget: int = 10_000, **overrides: str) -> str:
        suffix = uuid.uuid4().hex[:8]
        cid, cluster, project = f"ch-{suffix}", f"cl-{suffix}", f"proj-{suffix}"
        sql("INSERT INTO clusters (id, display_name) VALUES (%s, %s)", cluster, cluster)
        sql(
            "INSERT INTO google_projects (id, daily_quota_units, oauth_client_ref) "
            "VALUES (%s, %s, 'env://YT_OAUTH_CLIENT')",
            project,
            quota,
        )
        sql(
            "INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, cluster_id,"
            " autonomy_tier, daily_budget_cents, google_project_id, youtube_credentials_ref) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'env://YT_REFRESH_TOKEN')",
            cid, cid, f"channels/{cid}/", f"mem:{cid}", cluster,
            overrides.get("autonomy_tier", "gated"), budget, project,
        )  # fmt: skip
        return cid

    return make


@pytest.fixture
def secrets() -> Any:
    return fake_secrets()

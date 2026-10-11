"""Content-addressed assets in Postgres (staging only, 5 MiB each): the hub, driver and
gateway share them without a bucket. Production uses the object store."""

from __future__ import annotations

from typing import Any

import psycopg

from ytcore.approvals.hashing import file_digest
from ytcore.gateway.assets import AssetIntegrityError, asset_key


class PostgresAssetSource:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self._conninfo, autocommit=True)

    def put(self, storage_prefix: str, data: bytes) -> str:
        digest = file_digest(data)
        self.write_raw(asset_key(storage_prefix, digest), data)
        return digest

    def write_raw(self, key: str, data: bytes) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO asset_blobs (key, data) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (key, data),
            )

    def read(self, key: str) -> bytes:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM asset_blobs WHERE key = %s", (key,)).fetchone()
        if row is None:
            raise AssetIntegrityError(f"asset not found: {key}")
        return bytes(row[0])

    def list_keys(self, prefix: str = "") -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key FROM asset_blobs WHERE starts_with(key, %s) ORDER BY key", (prefix,)
            ).fetchall()
        return [r[0] for r in rows]

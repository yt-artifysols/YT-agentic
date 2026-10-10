"""Asset access for the gateway. Assets are content-addressed under the channel prefix."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ytcore.approvals.hashing import file_digest


class AssetIntegrityError(Exception):
    pass


class AssetSource(Protocol):
    def read(self, key: str) -> bytes: ...


class AssetSink(Protocol):
    def put(self, storage_prefix: str, data: bytes) -> str:
        """Store content-addressed under the channel prefix; return its sha256 digest."""
        ...


def asset_key(storage_prefix: str, digest: str) -> str:
    return f"{storage_prefix}assets/{digest.removeprefix('sha256:')}"


def read_verified(source: AssetSource, storage_prefix: str, digest: str) -> bytes:
    data = source.read(asset_key(storage_prefix, digest))
    if file_digest(data) != digest:
        raise AssetIntegrityError("asset bytes do not match the approved digest")
    return data


@dataclass
class InMemoryAssetSource:
    """Default asset store for dev and tests."""

    objects: dict[str, bytes] = field(default_factory=dict)

    def put(self, storage_prefix: str, data: bytes) -> str:
        digest = file_digest(data)
        self.objects[asset_key(storage_prefix, digest)] = data
        return digest

    def read(self, key: str) -> bytes:
        if key not in self.objects:
            raise AssetIntegrityError(f"asset not found: {key}")
        return self.objects[key]

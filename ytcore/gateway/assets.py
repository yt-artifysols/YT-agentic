"""Asset access for the gateway. Assets are content-addressed under the channel prefix."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
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


class ListableAssets(Protocol):
    """What the backup and restore tooling needs from an asset store."""

    def put(self, storage_prefix: str, data: bytes) -> str: ...

    def read(self, key: str) -> bytes: ...

    def list_keys(self, prefix: str = "") -> list[str]: ...

    def write_raw(self, key: str, data: bytes) -> None: ...


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

    def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def write_raw(self, key: str, data: bytes) -> None:
        self.objects[key] = data


class LocalDirAssetSource:
    """Asset store on a local directory (dev, staging-on-a-VM and restore drills). Keys are
    validated so a key can never escape the root."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key or "\0" in key:
            raise AssetIntegrityError(f"invalid asset key: {key!r}")
        path = (self._root / key).resolve()
        if self._root not in path.parents:
            raise AssetIntegrityError(f"invalid asset key: {key!r}")
        return path

    def put(self, storage_prefix: str, data: bytes) -> str:
        digest = file_digest(data)
        self.write_raw(asset_key(storage_prefix, digest), data)
        return digest

    def read(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise AssetIntegrityError(f"asset not found: {key}")
        return path.read_bytes()

    def write_raw(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(
            p.relative_to(self._root).as_posix()
            for p in self._root.rglob("*")
            if p.is_file()
            and not p.name.endswith(".tmp")
            and p.relative_to(self._root).as_posix().startswith(prefix)
        )


class GcsAssetSource:  # pragma: no cover - needs a real bucket (`gcp` extra)
    """Versioned object storage (Google Cloud Storage). Same key layout as every other store:
    channels/<channel_id>/assets/<sha256>. Objects are content-addressed, so an overwrite with
    different bytes is impossible by construction (the key is the digest)."""

    def __init__(self, bucket: str) -> None:
        from google.cloud import storage

        self._bucket = storage.Client().bucket(bucket)

    def put(self, storage_prefix: str, data: bytes) -> str:
        digest = file_digest(data)
        self.write_raw(asset_key(storage_prefix, digest), data)
        return digest

    def read(self, key: str) -> bytes:
        blob = self._bucket.blob(key)
        if not blob.exists():
            raise AssetIntegrityError(f"asset not found: {key}")
        return bytes(blob.download_as_bytes())

    def write_raw(self, key: str, data: bytes) -> None:
        self._bucket.blob(key).upload_from_string(data, if_generation_match=None)

    def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(b.name for b in self._bucket.list_blobs(prefix=prefix))

"""Approval signing keys. ONLY the approval hub may import this module.

KeyProvider hides where the Ed25519 private key lives:
- LocalFileKeyProvider: a PEM file on disk, dev only (refuses ENV=production).
- KmsKeyProvider: interface for a cloud KMS asymmetric key (sign happens inside the KMS, the
  private key never leaves it). Not wired yet.

The gateway, workers and agents only ever receive the PUBLIC key (ApprovalVerifier).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ytcore.secrets.provider import ProductionForbiddenError


@runtime_checkable
class KeyProvider(Protocol):
    def sign(self, data: bytes) -> bytes: ...

    def public_key(self) -> Ed25519PublicKey: ...


class InMemoryKeyProvider:
    """Tests only."""

    def __init__(self, key: Ed25519PrivateKey | None = None) -> None:
        if os.environ.get("ENV", "").strip().lower() == "production":
            raise ProductionForbiddenError("InMemoryKeyProvider refuses ENV=production")
        self._key = key or Ed25519PrivateKey.generate()

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)

    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()


class LocalFileKeyProvider:
    """DEV ONLY: reads an unencrypted PKCS8 PEM Ed25519 key from a git-ignored file."""

    def __init__(self, path: Path, *, env: str | None = None) -> None:
        for candidate in (env, os.environ.get("ENV")):
            if candidate is not None and candidate.strip().lower() == "production":
                raise ProductionForbiddenError(
                    "LocalFileKeyProvider is dev-only and refuses to run when ENV=production"
                )
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("approval key must be Ed25519")
        self._key = key

    @staticmethod
    def generate(path: Path) -> None:
        """Create a dev key file (mode 0600). Never commit it (*.pem is git-ignored)."""
        pem = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)

    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()


class KmsKeyProvider:
    """Production: asymmetric Ed25519 key held in a cloud KMS, referenced by resource name.

    Wire a KMS client (e.g. Cloud KMS asymmetricSign) in a later phase. The interface is final:
    signing happens remotely; the private key never enters this process.
    """

    def __init__(self, *, key_resource: str) -> None:
        if not key_resource:
            raise ValueError("KMS key resource name is required")
        self._key_resource = key_resource

    def sign(self, data: bytes) -> bytes:
        raise NotImplementedError("KMS signing is not wired yet")

    def public_key(self) -> Ed25519PublicKey:
        raise NotImplementedError("KMS public key fetch is not wired yet")

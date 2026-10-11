"""Approval signing keys. ONLY the approval hub may import this module.

KeyProvider hides where the Ed25519 private key lives:
- LocalFileKeyProvider: a PEM file on disk, dev only (refuses ENV=production).
- EnvKeyProvider: staging on serverless hosts; PEM from a platform secret (refuses production).
- KmsKeyProvider: interface for a cloud KMS asymmetric key (sign happens inside the KMS, the
  private key never leaves it); the client is injectable for tests.

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


class EnvKeyProvider:
    """STAGING ONLY (serverless hosts with no KMS): the private PEM comes from an encrypted
    platform secret held by the hub's project alone. Refuses ENV=production."""

    def __init__(self, var: str = "YT_HUB_SIGNING_KEY_PEM", *, env: str | None = None) -> None:
        for candidate in (env, os.environ.get("ENV")):
            if candidate is not None and candidate.strip().lower() == "production":
                raise ProductionForbiddenError("EnvKeyProvider refuses ENV=production; use KMS")
        pem = os.environ.get(var, "").replace("\\n", "\n").encode()
        if not pem:
            raise ValueError(f"{var} is not set")
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("approval key must be Ed25519")
        self._key = key

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)

    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()


class KmsClient(Protocol):
    """The two Cloud KMS calls we need. Signing happens inside the KMS."""

    def sign(self, key_version: str, data: bytes) -> bytes: ...

    def public_key_pem(self, key_version: str) -> str: ...


class GoogleKmsClient:  # pragma: no cover - needs Cloud KMS (`gcp` extra)
    def __init__(self) -> None:
        from google.cloud import kms

        self._client = kms.KeyManagementServiceClient()

    def sign(self, key_version: str, data: bytes) -> bytes:
        # Ed25519 signs the message itself (not a digest).
        return bytes(
            self._client.asymmetric_sign(request={"name": key_version, "data": data}).signature
        )

    def public_key_pem(self, key_version: str) -> str:
        return str(self._client.get_public_key(request={"name": key_version}).pem)


class KmsKeyProvider:
    """Production: an Ed25519 key held in Cloud KMS (EC_SIGN_ED25519), referenced by the full
    CryptoKeyVersion resource name. Signing happens remotely; the private key never enters this
    process, and the public key is fetched once and cached."""

    def __init__(self, *, key_resource: str, client: KmsClient | None = None) -> None:
        if not key_resource:
            raise ValueError("KMS key resource name is required")
        if "/cryptoKeyVersions/" not in key_resource:
            raise ValueError("KMS key resource must name a CryptoKeyVersion")
        self._key_resource = key_resource
        self._client = client or GoogleKmsClient()
        self._public: Ed25519PublicKey | None = None

    def sign(self, data: bytes) -> bytes:
        return self._client.sign(self._key_resource, data)

    def public_key(self) -> Ed25519PublicKey:
        if self._public is None:
            key = serialization.load_pem_public_key(
                self._client.public_key_pem(self._key_resource).encode()
            )
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError("the KMS key is not an Ed25519 key")
            self._public = key
        return self._public

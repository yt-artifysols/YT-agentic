from __future__ import annotations

import stat
from datetime import timedelta
from pathlib import Path

import pytest

from ytcore.approvals.keys import (
    InMemoryKeyProvider,
    KeyProvider,
    KmsKeyProvider,
    LocalFileKeyProvider,
)
from ytcore.approvals.token import ApprovalSigner, ApprovalVerifier
from ytcore.secrets.provider import ProductionForbiddenError

HASH = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENV", raising=False)


def test_local_key_file_roundtrip_and_permissions(tmp_path: Path) -> None:
    path = tmp_path / "k.pem"
    LocalFileKeyProvider.generate(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    provider = LocalFileKeyProvider(path, env="dev")
    assert isinstance(provider, KeyProvider)
    token, _ = ApprovalSigner(provider).issue(
        job_id="j", channel_id="c", content_hash=HASH, approver="a", ttl=timedelta(hours=1)
    )
    ApprovalVerifier(provider.public_key()).verify(
        token, job_id="j", channel_id="c", content_hash=HASH
    )
    with pytest.raises(FileExistsError):
        LocalFileKeyProvider.generate(path)  # never overwrite a key


def test_dev_key_providers_refuse_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "k.pem"
    LocalFileKeyProvider.generate(path)
    with pytest.raises(ProductionForbiddenError):
        LocalFileKeyProvider(path, env="production")
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(ProductionForbiddenError):
        LocalFileKeyProvider(path, env="dev")
    with pytest.raises(ProductionForbiddenError):
        InMemoryKeyProvider()


class FakeKms:
    """Stands in for Cloud KMS: holds the private key, exposes sign and the public PEM only."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._key = Ed25519PrivateKey.generate()
        self.signed = 0

    def sign(self, key_version: str, data: bytes) -> bytes:
        self.signed += 1
        return self._key.sign(data)

    def public_key_pem(self, key_version: str) -> str:
        from cryptography.hazmat.primitives import serialization

        return (
            self._key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )


VERSION = "projects/p/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1"


def test_kms_provider_signs_remotely_and_verifies_with_the_public_key_only() -> None:
    kms = KmsKeyProvider(key_resource=VERSION, client=FakeKms())
    signature = kms.sign(b"approval")
    kms.public_key().verify(signature, b"approval")
    with pytest.raises(Exception):  # noqa: B017 - any verification failure
        kms.public_key().verify(signature, b"tampered")
    assert not hasattr(kms, "_key")  # no private key material in this process


def test_kms_provider_needs_a_key_version_and_an_ed25519_key() -> None:
    with pytest.raises(ValueError, match="CryptoKeyVersion"):
        KmsKeyProvider(key_resource="projects/p/locations/l/keyRings/r/cryptoKeys/k",
                       client=FakeKms())  # fmt: skip

    class RsaKms(FakeKms):
        def public_key_pem(self, key_version: str) -> str:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa

            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            return (
                key.public_key()
                .public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
                )
                .decode()
            )

    with pytest.raises(ValueError, match="Ed25519"):
        KmsKeyProvider(key_resource=VERSION, client=RsaKms()).public_key()

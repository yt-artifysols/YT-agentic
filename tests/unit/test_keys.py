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


def test_kms_provider_is_an_interface_until_wired() -> None:
    kms = KmsKeyProvider(key_resource="projects/p/locations/l/keyRings/r/cryptoKeys/k")
    with pytest.raises(NotImplementedError):
        kms.sign(b"x")

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ytcore.approvals.hashing import content_hash, file_digest
from ytcore.approvals.token import ApprovalError, ApprovalSigner, ApprovalVerifier

HASH = content_hash({"metadata": {"title": "t"}, "assets": {"v.mp4": file_digest(b"video")}})
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def signer() -> ApprovalSigner:
    return ApprovalSigner(Ed25519PrivateKey.generate())


def _issue(signer: ApprovalSigner, **kw: object) -> str:
    args: dict[str, object] = {
        "job_id": "job-1",
        "channel_id": "chan-a",
        "content_hash": HASH,
        "approver": "owner",
        "ttl": timedelta(hours=1),
        "now": NOW,
    }
    args.update(kw)
    token, _ = signer.issue(**args)  # type: ignore[arg-type]
    return token


def test_content_hash_is_order_independent() -> None:
    a = content_hash({"metadata": {"a": 1, "b": 2}, "assets": {}})
    b = content_hash({"assets": {}, "metadata": {"b": 2, "a": 1}})
    assert a == b and a.startswith("sha256:")


def test_content_hash_changes_with_content() -> None:
    other = content_hash({"metadata": {"title": "t"}, "assets": {"v.mp4": file_digest(b"x")}})
    assert other != HASH


def test_valid_token_verifies(signer: ApprovalSigner) -> None:
    claims = ApprovalVerifier(signer.public_key()).verify(
        _issue(signer), job_id="job-1", channel_id="chan-a", content_hash=HASH, now=NOW
    )
    assert claims.approver == "owner"


@pytest.mark.parametrize(
    ("field", "value"),
    [("job_id", "job-2"), ("channel_id", "chan-b"), ("content_hash", "sha256:" + "0" * 64)],
)
def test_token_bound_to_job_channel_and_hash(
    signer: ApprovalSigner, field: str, value: str
) -> None:
    binding = {"job_id": "job-1", "channel_id": "chan-a", "content_hash": HASH, field: value}
    with pytest.raises(ApprovalError):
        ApprovalVerifier(signer.public_key()).verify(_issue(signer), now=NOW, **binding)


def test_expired_token_rejected(signer: ApprovalSigner) -> None:
    with pytest.raises(ApprovalError, match="expired"):
        ApprovalVerifier(signer.public_key()).verify(
            _issue(signer),
            job_id="job-1",
            channel_id="chan-a",
            content_hash=HASH,
            now=NOW + timedelta(hours=2),
        )


def test_forged_token_rejected(signer: ApprovalSigner) -> None:
    forger = ApprovalSigner(Ed25519PrivateKey.generate())
    with pytest.raises(ApprovalError, match="signature"):
        ApprovalVerifier(signer.public_key()).verify(
            _issue(forger), job_id="job-1", channel_id="chan-a", content_hash=HASH, now=NOW
        )


def test_tampered_claims_rejected(signer: ApprovalSigner) -> None:
    body, sig = _issue(signer).split(".")
    tampered = body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + sig
    with pytest.raises(ApprovalError):
        ApprovalVerifier(signer.public_key()).verify(
            tampered, job_id="job-1", channel_id="chan-a", content_hash=HASH, now=NOW
        )


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(days=30)])
def test_ttl_bounds(signer: ApprovalSigner, ttl: timedelta) -> None:
    with pytest.raises(ApprovalError):
        _issue(signer, ttl=ttl)

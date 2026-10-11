"""Ed25519 approval tokens.

The approval hub alone holds the private key (ApprovalSigner). The YouTube gateway holds only the
public key (ApprovalVerifier). A token is bound to job_id, channel_id and content_hash and expires.

Format: base64url(canonical JSON claims) "." base64url(signature over the claims bytes).
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ytcore.approvals.hashing import canonical_json, is_content_hash

TOKEN_VERSION = 1
MAX_TTL = timedelta(hours=72)


class _Signs(Protocol):
    def sign(self, data: bytes) -> bytes: ...

    def public_key(self) -> Ed25519PublicKey: ...


class ApprovalError(Exception):
    """Raised for any token that must not authorize a publish."""


@dataclass(frozen=True, slots=True)
class ApprovalClaims:
    jti: str  # unique token id; consumed once in the approvals table
    job_id: str
    channel_id: str
    content_hash: str
    approver: str
    issued_at: datetime
    expires_at: datetime

    def to_json(self) -> dict[str, object]:
        return {
            "v": TOKEN_VERSION,
            "jti": self.jti,
            "job": self.job_id,
            "chan": self.channel_id,
            "hash": self.content_hash,
            "by": self.approver,
            "iat": int(self.issued_at.timestamp()),
            "exp": int(self.expires_at.timestamp()),
        }

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> ApprovalClaims:
        try:
            if raw["v"] != TOKEN_VERSION:
                raise ApprovalError("unsupported token version")
            iat, exp = raw["iat"], raw["exp"]
            if not isinstance(iat, int) or not isinstance(exp, int):
                raise ApprovalError("malformed timestamps")
            return cls(
                jti=str(raw["jti"]),
                job_id=str(raw["job"]),
                channel_id=str(raw["chan"]),
                content_hash=str(raw["hash"]),
                approver=str(raw["by"]),
                issued_at=datetime.fromtimestamp(iat, UTC),
                expires_at=datetime.fromtimestamp(exp, UTC),
            )
        except KeyError as exc:
            raise ApprovalError(f"missing claim {exc}") from None


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ApprovalError("malformed token encoding") from None


class ApprovalSigner:
    """Approval hub only. Never construct this in the gateway, workers or agents.

    Takes a KeyProvider (ytcore/approvals/keys.py) or, in tests, an Ed25519PrivateKey; both
    expose sign() and public_key().
    """

    def __init__(self, key: Ed25519PrivateKey | _Signs) -> None:
        self._key = key

    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()

    def issue(
        self,
        *,
        job_id: str,
        channel_id: str,
        content_hash: str,
        approver: str,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> tuple[str, ApprovalClaims]:
        if not is_content_hash(content_hash):
            raise ApprovalError("content_hash must be sha256:<64 hex>")
        if not approver:
            raise ApprovalError("approver is required")
        if not timedelta(0) < ttl <= MAX_TTL:
            raise ApprovalError(f"ttl must be within (0, {MAX_TTL}]")
        issued = (now or datetime.now(UTC)).replace(microsecond=0)
        claims = ApprovalClaims(
            jti=secrets.token_urlsafe(16),
            job_id=job_id,
            channel_id=channel_id,
            content_hash=content_hash,
            approver=approver,
            issued_at=issued,
            expires_at=issued + ttl,
        )
        body = canonical_json(claims.to_json())
        return f"{_b64e(body)}.{_b64e(self._key.sign(body))}", claims


class ApprovalVerifier:
    """Holds only the public key. Used by the YouTube gateway before any upload."""

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self._key = public_key

    def verify(
        self,
        token: str,
        *,
        job_id: str,
        channel_id: str,
        content_hash: str,
        now: datetime | None = None,
    ) -> ApprovalClaims:
        parts = token.split(".")
        if len(parts) != 2:
            raise ApprovalError("malformed token")
        body, sig = _b64d(parts[0]), _b64d(parts[1])
        try:
            self._key.verify(sig, body)
        except InvalidSignature:
            raise ApprovalError("invalid signature") from None
        try:
            raw = json.loads(body)
        except ValueError:
            raise ApprovalError("malformed claims") from None
        if not isinstance(raw, dict):
            raise ApprovalError("malformed claims")
        claims = ApprovalClaims.from_json(raw)
        if claims.job_id != job_id:
            raise ApprovalError("token is bound to a different job")
        if claims.channel_id != channel_id:
            raise ApprovalError("token is bound to a different channel")
        if claims.content_hash != content_hash:
            raise ApprovalError("token is bound to different content")
        current = now or datetime.now(UTC)
        if current >= claims.expires_at:
            raise ApprovalError("token expired")
        if claims.issued_at > current + timedelta(minutes=5):
            raise ApprovalError("token issued in the future")
        return claims

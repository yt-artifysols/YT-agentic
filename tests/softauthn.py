"""A minimal software WebAuthn authenticator (ES256, "none" attestation) for tests.

It produces the same JSON a browser's navigator.credentials.create()/get() would send, so the
hub's py_webauthn verification runs unmodified.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, encode_cbor

FLAG_UP, FLAG_UV, FLAG_AT = 0x01, 0x04, 0x40


class SoftAuthenticator:
    def __init__(self, rp_id: str, origin: str) -> None:
        self.rp_id, self.origin = rp_id, origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}
        ).encode()

    def _rp_hash(self) -> bytes:
        return hashlib.sha256(self.rp_id.encode()).digest()

    def _cose_key(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return encode_cbor(
            {1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")}
        )

    def create(self, options: dict[str, Any]) -> dict[str, Any]:
        client_data = self._client_data("webauthn.create", options["challenge"])
        attested = (
            b"\x00" * 16
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose_key()
        )
        auth_data = (
            self._rp_hash()
            + bytes([FLAG_UP | FLAG_UV | FLAG_AT])
            + struct.pack(">I", self.sign_count)
            + attested
        )
        att_obj = encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid,
            "rawId": cid,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(att_obj),
                "transports": ["internal"],
            },
        }

    def get(self, options: dict[str, Any], *, user_verified: bool = True) -> dict[str, Any]:
        allowed = [base64url_to_bytes(c["id"]) for c in options.get("allowCredentials", [])]
        if allowed and self.credential_id not in allowed:
            raise AssertionError("authenticator not allowed for this ceremony")
        self.sign_count += 1
        client_data = self._client_data("webauthn.get", options["challenge"])
        flags = FLAG_UP | (FLAG_UV if user_verified else 0)
        auth_data = self._rp_hash() + bytes([flags]) + struct.pack(">I", self.sign_count)
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid,
            "rawId": cid,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": None,
            },
        }

"""Deterministic content hashing for approval binding.

The hash covers the exact publishable package (metadata plus the digest of every asset), so an
approval for one package can never authorize a different one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def file_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def content_hash(package: Mapping[str, Any]) -> str:
    """Hash a publish package: {"metadata": {...}, "assets": {"name": "sha256:..."}}."""
    assets = package.get("assets", {})
    for name, digest in assets.items():
        if not isinstance(digest, str) or not CONTENT_HASH_RE.match(digest):
            raise ValueError(f"asset {name!r} must carry a sha256 digest")
    return "sha256:" + hashlib.sha256(canonical_json(package)).hexdigest()


def is_content_hash(value: str) -> bool:
    return bool(CONTENT_HASH_RE.match(value))

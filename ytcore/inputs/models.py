from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ytcore.approvals.hashing import file_digest
from ytcore.safety.scrub import ensure_no_secrets

URI_RE = re.compile(r"^(file|gs|s3|https)://\S{1,2000}$")


class InputKind(StrEnum):
    IDEA = "idea"
    FOOTAGE = "footage"
    NOTE = "note"
    GOAL = "goal"  # standing context (target audience, cadence, growth focus); never consumed


CONSUMABLE = frozenset({InputKind.IDEA, InputKind.FOOTAGE, InputKind.NOTE})


@dataclass(frozen=True)
class OwnerInput:
    id: str
    channel_id: str
    kind: InputKind
    uri: str | None
    body: str | None
    sha256: str
    status: str  # new | used | archived
    submitted_by: str
    used_by_job: str | None = None
    created_at: datetime | None = None


def body_digest(body: str) -> str:
    return file_digest(body.encode("utf-8"))


def validate_input(kind: InputKind, body: str | None, uri: str | None, sha256: str | None) -> str:
    """Validate a submission and return its sha256 (computed from the body when there is no uri)."""
    if body is None and uri is None:
        raise ValueError("an owner input needs a body or a uri")
    if kind is InputKind.FOOTAGE and uri is None:
        raise ValueError("footage needs a uri")
    if uri is not None and not URI_RE.match(uri):
        raise ValueError("uri must be file://, gs://, s3:// or https://")
    if body is not None and not 1 <= len(body) <= 20_000:
        raise ValueError("body must be 1..20000 characters")
    if body is not None:
        ensure_no_secrets(body, "an owner input")
    if uri is None:
        assert body is not None
        digest = body_digest(body)
        if sha256 is not None and sha256 != digest:
            raise ValueError("sha256 does not match the body")
        return digest
    if sha256 is None or not re.fullmatch(r"sha256:[0-9a-f]{64}", sha256):
        raise ValueError("inputs with a uri need the sha256 of the referenced content")
    return sha256

"""Limits for generated, stored and uploaded assets: size caps, media-type allowlists and a
deny-list of dangerous content signatures. Applied when an agent stores an asset and when the
gateway reads the approved video, so an oversized or disguised file never gets further."""

from __future__ import annotations

from dataclasses import dataclass

MiB = 1024 * 1024


class AssetRejected(ValueError):
    pass


@dataclass(frozen=True)
class AssetLimits:
    max_bytes: dict[str, int]
    media_types: dict[str, frozenset[str]]

    @classmethod
    def default(cls) -> AssetLimits:
        return cls(
            max_bytes={
                "video": 2048 * MiB,
                "audio": 200 * MiB,
                "music": 100 * MiB,
                "stock": 1024 * MiB,
                "image": 20 * MiB,
                "document": 5 * MiB,
            },
            media_types={
                "video": frozenset({"video/mp4", "video/webm", "video/quicktime"}),
                "audio": frozenset({"audio/wav", "audio/mpeg", "audio/mp4", "audio/ogg"}),
                "music": frozenset({"audio/wav", "audio/mpeg", "audio/mp4", "audio/ogg"}),
                "stock": frozenset(
                    {"video/mp4", "video/webm", "video/quicktime", "image/png", "image/jpeg"}
                ),
                "image": frozenset({"image/png", "image/jpeg", "image/webp"}),
                "document": frozenset({"application/json", "text/plain"}),
            },
        )


# Signatures that never belong in a media file: executables, scripts, markup and archives.
_DENY: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "Windows executable"),
    (b"\x7fELF", "ELF executable"),
    (b"\xca\xfe\xba\xbe", "Mach-O/Java class"),
    (b"#!", "script"),
    (b"PK\x03\x04", "zip archive"),
    (b"\x1f\x8b", "gzip archive"),
    (b"Rar!", "rar archive"),
    (b"7z\xbc\xaf", "7z archive"),
    (b"%PDF", "PDF document"),
)
_MARKUP = (b"<!doctype", b"<html", b"<svg", b"<script", b"<?php", b"<?xml", b"<iframe")


def check_asset(kind: str, media_type: str, data: bytes, limits: AssetLimits | None = None) -> None:
    limits = limits or AssetLimits.default()
    if kind not in limits.max_bytes:
        raise AssetRejected(f"unknown asset kind {kind!r}")
    if media_type not in limits.media_types[kind]:
        raise AssetRejected(f"media type {media_type!r} is not allowed for {kind} assets")
    if len(data) > limits.max_bytes[kind]:
        raise AssetRejected(
            f"{kind} asset is {len(data)} bytes; the limit is {limits.max_bytes[kind]}"
        )
    if not data:
        raise AssetRejected("empty asset")
    head = data[:512]
    for signature, label in _DENY:
        if head.startswith(signature):
            raise AssetRejected(f"{label} content disguised as {media_type}")
    if kind != "document":
        lowered = head.lstrip().lower()
        if any(lowered.startswith(marker) for marker in _MARKUP) or b"<script" in head.lower():
            raise AssetRejected(f"markup or script content disguised as {media_type}")


def check_size(kind: str, size: int, limits: AssetLimits | None = None) -> None:
    limits = limits or AssetLimits.default()
    if size > limits.max_bytes.get(kind, 0):
        raise AssetRejected(
            f"{kind} asset is {size} bytes; the limit is {limits.max_bytes.get(kind, 0)}"
        )

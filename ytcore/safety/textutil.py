"""Shared text helpers for the safety rules."""

from __future__ import annotations

import re

STOP = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "for", "is", "are", "was",
        "were", "be", "been", "by", "with", "that", "this", "these", "those", "it", "its", "as",
        "at", "from", "about", "into", "than", "then", "so", "what", "why", "how", "who", "when",
        "where", "which", "you", "your", "we", "our", "they", "their", "i", "he", "she", "his",
        "her", "not", "no", "do", "does", "did", "can", "could", "will", "would", "should",
    }
)  # fmt: skip
TOKEN = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    return TOKEN.findall(text.lower())

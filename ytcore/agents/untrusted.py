"""Untrusted content handling.

Scraped pages, owner-submitted text, research notes and model outputs are DATA. They only reach
a model inside a delimited block whose markers the content cannot forge (marker characters are
neutralised inside the content), and the trusted system prompt says that nothing inside a block
is an instruction. Agents never execute tool calls proposed by a model: tools are invoked by
code from a fixed allowlist (tools.py), and model output is parsed into strict schemas.
"""

from __future__ import annotations

import re
import secrets
import unicodedata

from ytcore.safety.scrub import scrub

OPEN = "<<<UNTRUSTED_DATA"
CLOSE = "<<<END_UNTRUSTED_DATA"
_BLOCK = re.compile(
    r"<<<UNTRUSTED_DATA label=\"(?P<label>[^\"]*)\" id=(?P<id>[0-9a-f]+)>>>\n"
    r"(?P<body>.*?)\n<<<END_UNTRUSTED_DATA id=(?P=id)>>>",
    re.S,
)
MAX_BLOCK_CHARS = 12_000

# Signals that a piece of data is trying to act as instructions. Detection only feeds QA flags;
# safety never depends on it (the block structure does).
INJECTION_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts?)",
        r"disregard (all |any |the )?(previous|prior|above|system)",
        r"\byou are now\b",
        r"\bsystem prompt\b",
        r"\bnew instructions?\b",
        r"<\s*/?\s*(tool_call|function_call|tool_use|system)\b",
        r"\"(tool|function|tool_name)\"\s*:",
        r"\b(approval[_ ]token|approved for publish(ing)?|auto[- ]?publish)\b",
        r"\bpublish (it|this|now|immediately)\b",
        r"<<<",
    )
] + [re.compile(r"\bAPPROVED\b")]  # shouting "APPROVED" (case-sensitive on purpose)


def _neutralise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    # Drop control characters except newline and tab.
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    # Credentials pasted into (or planted in) external content never reach a model.
    text = scrub(text)
    # Make the block markers impossible to forge from inside the content.
    return text.replace("<<<", "\u2039\u2039\u2039").replace(">>>", "\u203a\u203a\u203a")


def data_block(label: str, content: str, *, block_id: str | None = None) -> str:
    """Wrap untrusted content in a delimited, unforgeable data block."""
    bid = block_id or secrets.token_hex(6)
    safe_label = re.sub(r"[^A-Za-z0-9 _.:-]", "", label)[:60]
    body = _neutralise(content)[:MAX_BLOCK_CHARS]
    return f'{OPEN} label="{safe_label}" id={bid}>>>\n{body}\n{CLOSE} id={bid}>>>'


def extract_blocks(text: str) -> list[tuple[str, str]]:
    """(label, content) of every data block in a prompt."""
    return [(m["label"], m["body"]) for m in _BLOCK.finditer(text)]


def outside_blocks(text: str) -> str:
    """The prompt with every data block removed: the part a model may treat as instructions."""
    return _BLOCK.sub("", text)


def injection_signals(text: str) -> list[str]:
    return [p.pattern for p in INJECTION_PATTERNS if p.search(text)]


def clean_text(text: str, *, limit: int) -> str:
    """Normalise untrusted text for storage (no control characters, bounded)."""
    return _neutralise(text)[:limit]

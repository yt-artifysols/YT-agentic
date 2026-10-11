"""Secret scrubbing: a log record factory, a prompt/content scrubber and a detector.

`find_secrets` is the single list of secret shapes: the log scrubber, the prompt scrubber
(untrusted content), the intake checks for owner inputs and knowledge, and the repo scan test all
use it. It over-matches on purpose; a redaction is cheap, a leaked key is not.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

REDACTED = "[REDACTED:{}]"


@dataclass(frozen=True)
class SecretMatch:
    name: str
    start: int
    end: int


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx, flags))
    for name, rx, flags in (
        ("anthropic-key", r"sk-ant-[A-Za-z0-9_\-]{20,}", 0),
        ("openai-style-key", r"\bsk-[A-Za-z0-9]{24,}", 0),
        ("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", 0),
        ("google-api-key", r"\bAIza[0-9A-Za-z_\-]{35}\b", 0),
        ("google-refresh-token", r"\b1//[0-9A-Za-z_\-]{30,}", 0),
        ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})", 0),
        ("slack-token", r"\bxox[abprs]-[A-Za-z0-9\-]{10,}", 0),
        ("slack-webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,}", 0),
        ("stripe-key", r"\b[sr]k_(?:live|test)_[0-9a-zA-Z]{16,}", 0),
        (
            "jwt",
            r"\beyJ[A-Za-z0-9_\-]{10,2000}\.[A-Za-z0-9_\-]{10,2000}\.[A-Za-z0-9_\-]{5,2000}",
            0,
        ),
        (
            "private-key-block",
            r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----[\s\S]{0,6000}?"
            r"(?:-----END [A-Z ]{0,20}PRIVATE KEY-----|$)",
            0,
        ),
        ("bearer-token", r"\bBearer\s+[A-Za-z0-9._~+/\-]{20,}=*", re.I),
        # Our approval tokens: base64url(claims).base64url(signature).
        (
            "approval-token",
            r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{40,512}\.[A-Za-z0-9_\-]{40,512}(?![A-Za-z0-9_\-])",
            0,
        ),
        ("url-credentials", r"://[^/\s:@]{1,64}:[^/\s@]{3,}@", 0),
        (
            "assigned-secret",
            r"(?:api[_-]?key|secret|token|passw(?:or)?d|refresh[_-]?token|client[_-]?secret|"
            r"authorization)[\"']?\s*[:=]\s*[\"']?[^\s\"',;]{8,}",
            re.I,
        ),
    )
)


# Long inputs are scanned in overlapping windows so a hostile page cannot make the cost grow
# faster than linearly with its size (every pattern is also bounded).
_WINDOW = 50_000
_OVERLAP = 8_192


def _raw_spans(text: str) -> list[SecretMatch]:
    spans: list[SecretMatch] = []
    for offset in range(0, max(len(text), 1), _WINDOW):
        chunk = text[offset : offset + _WINDOW + _OVERLAP]
        for name, rx in _PATTERNS:
            spans.extend(
                SecretMatch(name, offset + m.start(), offset + m.end()) for m in rx.finditer(chunk)
            )
    return spans


def find_secrets(text: str) -> list[SecretMatch]:
    """Every secret-shaped span in the text (overlapping spans are merged)."""
    spans = _raw_spans(text)
    spans.sort(key=lambda s: (s.start, -s.end))
    merged: list[SecretMatch] = []
    for span in spans:
        if merged and span.start < merged[-1].end:
            continue
        merged.append(span)
    return merged


def contains_secret(text: str) -> bool:
    return bool(find_secrets(text))


def scrub(text: str) -> str:
    """Replace secret-shaped spans with a marker naming the kind of secret."""
    out, pos = [], 0
    for span in find_secrets(text):
        out.append(text[pos : span.start])
        out.append(REDACTED.format(span.name))
        pos = span.end
    out.append(text[pos:])
    return "".join(out)


class SecretInInput(ValueError):
    """Text submitted to the platform looks like a credential."""


def ensure_no_secrets(text: str, what: str) -> None:
    matches = find_secrets(text)
    if matches:
        raise SecretInInput(
            f"{what} contains something that looks like a credential ({matches[0].name}); "
            "never paste secrets here, use a vault reference instead"
        )


# ----------------------------------------------------------------------------- logging

_installed = False


def install_log_scrubber() -> None:
    """Scrub every log record (message, arguments and tracebacks) process-wide. Idempotent.

    A record factory is used, not a handler filter, so records from every logger are scrubbed
    whatever handlers are attached later."""
    global _installed
    if _installed:
        return
    _installed = True
    previous = logging.getLogRecordFactory()

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = scrub(message)
        record.args = None
        return record

    logging.setLogRecordFactory(factory)

    original = logging.Formatter.formatException

    def format_exception(self: logging.Formatter, ei: object) -> str:
        return scrub(original(self, ei))  # type: ignore[arg-type]

    logging.Formatter.formatException = format_exception  # type: ignore[method-assign]

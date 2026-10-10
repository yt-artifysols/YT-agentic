"""Keyword lexicons for sensitive topics and policy rules. Deliberately simple and auditable:
a human reviews these (docs/policy-sources.md). A hit means "look closer", never "unsafe"."""

from __future__ import annotations

import re

from ytcore.safety.policy import SENSITIVE_TOPICS, SensitiveTopic


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I)


SENSITIVE: dict[SensitiveTopic, re.Pattern[str]] = {
    "health": _rx(
        r"\bcure[sd]?\b",
        r"\bcancer\b",
        r"\bvaccin\w*",
        r"\bdiabet\w*",
        r"\bmedication\w*",
        r"\bprescription\w*",
        r"\bdosage\b",
        r"\bsymptom\w*",
        r"\bdiagnos\w+",
        r"\bsupplement\w*",
        r"\btherap(?:y|ies|ist)\b",
        r"\bdisease\w*",
        r"\bcovid\b",
        r"\bdepress\w+",
        r"\banxiety\b",
        r"\bweight loss\b",
        r"\bdoctor\w*",
        r"\bmedical\b",
    ),
    "finance": _rx(
        r"\binvest\w*",
        r"\bstocks?\b",
        r"\bcrypto\w*",
        r"\bbitcoin\b",
        r"\betfs?\b",
        r"\bindex funds?\b",
        r"\bretirement\b",
        r"\bloans?\b",
        r"\bmortgages?\b",
        r"\btrading\b",
        r"\bforex\b",
        r"\bportfolio\w*",
        r"\bdividends?\b",
        r"\bcredit scores?\b",
        r"\btaxes\b",
        r"\binterest rates?\b",
        r"\bsavings\b",
        r"\bbonds?\b",
    ),
    "politics": _rx(
        r"\belections?\b",
        r"\bvot(?:e|es|ing|ers?)\b",
        r"\bcandidates?\b",
        r"\bpresident\w*",
        r"\bparliament\w*",
        r"\bcongress\w*",
        r"\bsenat\w+",
        r"\bpolitical part(?:y|ies)\b",
        r"\breferendum\b",
        r"\bballots?\b",
        r"\bgovernment policy\b",
    ),
    "minors": _rx(
        r"\bchild(?:ren)?\b",
        r"\bkids?\b",
        r"\bminors?\b",
        r"\bteen(?:ager)?s?\b",
        r"\bunder 18\b",
        r"\bunderage\b",
        r"\btoddlers?\b",
        r"\binfants?\b",
        r"\bbab(?:y|ies)\b",
    ),
    "breaking_news": _rx(
        r"\bbreaking(?: news)?\b",
        r"\bjust announced\b",
        r"\bdeveloping story\b",
        r"\blive updates?\b",
        r"\bhappening now\b",
        r"\bmoments ago\b",
        r"\bthis just in\b",
    ),
}
assert set(SENSITIVE) == set(SENSITIVE_TOPICS)

# Contribution to the risk score when a sensitive topic is present and not allowed.
SENSITIVE_WEIGHT: dict[SensitiveTopic, float] = {
    "health": 0.30,
    "finance": 0.20,
    "politics": 0.30,
    "minors": 0.40,
    "breaking_news": 0.25,
}

SCAM = _rx(
    r"get rich quick",
    r"guaranteed (?:income|profit|returns?)",
    r"double your (?:money|investment)",
    r"risk[- ]?free (?:profit|returns?)",
    r"passive income while you sleep",
    r"\bdm me\b",
    r"send (?:me )?(?:crypto|bitcoin)",
    r"limited[- ]time offer",
    r"act now",
)
MEDICAL_CLAIM = _rx(
    r"\bcures?\b[^.]{0,40}\b(?:cancer|diabetes|covid|autism)\b",
    r"stop taking (?:your )?(?:medication|insulin|meds)",
    r"instead of (?:seeing )?(?:a )?(?:doctor|physician)",
    r"vaccines? (?:cause|causes|are poison)",
    r"\bbleach\b[^.]{0,30}\b(?:cure|prevent|drink)\b",
)
AI_EXPERT_PERSONA = _rx(
    r"\bas (?:a|your) (?:licensed |certified )?(?:doctor|physician|nurse|lawyer|attorney|"
    r"financial advis[eo]r|therapist|pharmacist|tax advis[eo]r)\b",
    r"\bi am (?:a|your) (?:licensed |certified )?(?:doctor|physician|lawyer|attorney|"
    r"financial advis[eo]r|therapist)\b",
)
CLICKBAIT = _rx(
    r"you won'?t believe",
    r"\bshocking\b",
    r"secret (?:they|that) (?:don'?t|doesn'?t) want",
    r"\bexposed\b",
    r"doctors hate",
    r"what happens next",
    r"\bgone wrong\b",
    r"\b100% (?:guaranteed|works)\b",
    r"\bnumber \d+ will\b",
    r"\bmust watch\b",
)


def sensitive_topics(text: str) -> list[SensitiveTopic]:
    return [topic for topic in SENSITIVE_TOPICS if SENSITIVE[topic].search(text)]

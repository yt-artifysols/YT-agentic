"""Agent pools v1: research, script_seo, media and qa.

Specialist roles (hook writer, caption writer, thumbnail maker, B-roll finder, brand checker)
are skills inside these four pools, not separate services. Every agent has typed input/output,
a versioned prompt file under prompts/, an explicit tool allowlist (tools.py) and metered,
budgeted provider calls (costs.py). External content only ever reaches a model inside
delimited untrusted-data blocks (untrusted.py).
"""

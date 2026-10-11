"""Safety and compliance: risk scoring, deterministic policy checks, cross-channel duplicate
detection, the cluster circuit breaker, asset limits and secrets hygiene.

Rules are derived from the official YouTube policies listed in docs/policy-sources.md. A passing
check never means a video is compliant; it only means no rule fired. A human always approves.
"""

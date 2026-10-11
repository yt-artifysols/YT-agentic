"""Helpers for the 20-channel simulation: a generated fleet config and fault-injecting providers."""

from __future__ import annotations

import itertools
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ytcore.agents.fake import FakeImage

ZONES = [
    "America/New_York", "America/Los_Angeles", "Europe/London", "Europe/Berlin",
    "Asia/Kolkata", "Asia/Tokyo", "Australia/Sydney", "America/Sao_Paulo",
]  # fmt: skip

SUBJECTS = [
    "sourdough starters",
    "drystone walls",
    "backyard telescopes",
    "mechanical keyboards",
    "bonsai pruning",
    "bicycle gearing",
    "fermented hot sauce",
    "wood joinery",
    "tide pools",
    "vintage cameras",
    "indoor herbs",
    "knot tying",
    "paper airplanes",
    "cast iron pans",
    "beekeeping basics",
    "soldering circuits",
    "watercolor washes",
    "trail navigation",
    "compost bins",
    "rock climbing knots",
    "pottery glazes",
    "ham radio antennas",
    "crossword construction",
    "chess openings",
    "kite building",
    "lighthouse history",
    "origami tessellations",
    "espresso extraction",
    "terrarium layers",
    "birdsong identification",
    "leather stitching",
    "stargazing apps",
    "podcast microphones",
    "tool sharpening",
    "pressure canning",
    "model railways",
    "typography pairing",
    "moss gardening",
    "woodturning bowls",
    "mushroom spore prints",
    "kayak paddling",
    "street photography",
    "linocut printing",
    "weather balloons",
    "map projections",
    "bread scoring",
    "lock mechanisms",
    "violin rosin",
    "glass blowing",
    "seed saving",
]
ANGLES = [
    "what beginners overlook",
    "three mistakes and their fixes",
    "a field guide for small spaces",
    "the history nobody tells you",
    "tools that are worth buying",
    "a week of practice plan",
    "how experts check their work",
    "myths worth retiring",
    "cheap versus careful choices",
    "a cold-weather checklist",
]


def topic(i: int, d: int) -> str:
    """A unique, non-sensitive topic per (channel index, day)."""
    rng = random.Random(i * 1000 + d)  # noqa: S311 - deterministic test data
    return f"{rng.choice(SUBJECTS)}: {rng.choice(ANGLES)} (episode {i}-{d})"


def write_fleet(root: Path, clusters: int = 4, per_cluster: int = 5) -> list[str]:
    (root / "clusters").mkdir(parents=True, exist_ok=True)
    (root / "channels").mkdir(exist_ok=True)
    slugs: list[str] = []
    for k in range(clusters):
        cid = f"load-{chr(97 + k)}"
        (root / "clusters" / f"{cid}.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": cid,
                    "display_name": f"Load cluster {k}",
                    "niche": "hobby explainers",
                    "live": True,
                    "slot_spacing_minutes": 20,
                    "max_concurrent_jobs": 3,
                    "daily_budget_cents": 20_000,
                    "google_projects": [{"id": f"ytcore-load-{k}-1", "daily_quota_units": 200_000}],
                    "pools": {"research": 3, "script_seo": 3, "media": 2, "gpu_render": 2, "qa": 3},
                }
            )
        )
        for j in range(per_cluster):
            n = k * per_cluster + j
            slug = f"load-{chr(97 + k)}{j}"
            slugs.append(slug)
            (root / "channels" / f"{slug}.yaml").write_text(
                yaml.safe_dump(
                    {
                        "slug": slug,
                        "display_name": f"Load channel {n}",
                        "cluster": cid,
                        "autonomy_tier": "gated",
                        "audience_timezone": ZONES[n % len(ZONES)],
                        "publish": {
                            "hour_local": 15 + (n % 6),
                            "window_minutes": 120,
                            "approval_lead_minutes": 60,
                        },
                        "budget": {"job_cents": 500, "daily_cents": 2_000},
                        "brand": {
                            "brand_voice": "Calm, precise and friendly. Plain words, no hype.\n",
                            "hook_library": "Most people get {topic} wrong.\n"
                            "Here is what the evidence says about {topic}.\n",
                            "banned_claims": "guaranteed results\n",
                        },
                        "policy": {
                            # The fake generators emit templated text by design; the real
                            # thresholds are covered by the safety and red-team suites.
                            "safety": {
                                "duplicates": {
                                    "shingle_escalate": 0.99,
                                    "shingle_block": 1.0,
                                    "embedding_escalate": 0.999,
                                    "embedding_block": 1.0,
                                    "title_template_escalate": 1000,
                                    "title_template_block": 2000,
                                    "thumbnail_text_escalate": 1000,
                                    "thumbnail_text_block": 2000,
                                }
                            },
                            "production": {
                                "min_sources": 1,
                                "tone_words": ["evidence"],
                                "forbidden_words": ["hype"],
                            },
                        },
                    }
                )
            )
    (root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "pools": {"gpu_render": 4, "research": 10, "script_seo": 10, "media": 6, "qa": 10},
                "targets": {
                    "min_success_rate": 0.97,
                    "max_cost_per_video_cents": 150,
                    "max_approval_queue_depth": 25,
                    "max_sim_day_seconds": 150,
                },
            }
        )
    )
    return slugs


class ProviderRateLimited(ConnectionError):
    """What a real provider's 429 looks like to the worker: a transient, retryable error."""


@dataclass
class Faults:
    seed: int = 7
    rate_limit_p: float = 0.03
    error_p: float = 0.01
    injected: Counter[str] = field(default_factory=Counter)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)  # noqa: S311 - deterministic fault injection

    def maybe(self, where: str) -> None:
        roll = self.rng.random()
        if roll < self.rate_limit_p:
            self.injected[f"{where}:rate_limit"] += 1
            raise ProviderRateLimited(f"{where}: 429 too many requests")
        if roll < self.rate_limit_p + self.error_p:
            self.injected[f"{where}:error"] += 1
            raise ConnectionError(f"{where}: upstream unavailable")


class UniqueImage:
    """Fake image provider whose output differs per call (real generators never repeat pixels),
    so the cross-channel reused-media check judges the pipeline, not the fake."""

    name = "fake-image"

    def __init__(self) -> None:
        self._n = itertools.count()

    def estimate_cents(self, prompt: str) -> int:
        return 4

    def generate(self, prompt: str, *, size: str = "1280x720") -> Any:
        return FakeImage().generate(f"{prompt} #{next(self._n)}", size=size)


class Flaky:
    """Forwards everything to a real fake, failing some calls the way providers do."""

    def __init__(self, inner: Any, faults: Faults, where: str, methods: tuple[str, ...]) -> None:
        self._inner, self._faults, self._where, self._methods = inner, faults, where, methods

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name not in self._methods:
            return attr

        def wrapped(*a: Any, **kw: Any) -> Any:
            self._faults.maybe(self._where)
            return attr(*a, **kw)

        return wrapped

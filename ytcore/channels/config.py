"""Schema and loader for config/clusters/*.yaml and config/channels/*.yaml.

Adding a channel is a new YAML file and `ytctl channel onboard <slug>`: no code changes. The
loader validates everything up front (schema, references between files, quota arithmetic, the
20-channel cap, no credential-shaped strings) so a bad file fails in CI, not in production.

Brand profile and policy are applied as knowledge DRAFTS; a human approves them in the hub.
Config can start a channel in `shadow`; going `gated` (publishing) needs its cluster to be live.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ytcore.knowledge.models import KnowledgeKind
from ytcore.safety.policy import SafetyPolicy
from ytcore.safety.scrub import SecretInInput, ensure_no_secrets

SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
POOLS = ("research", "script_seo", "media", "gpu_render", "qa")
UPLOAD_QUOTA_UNITS = 1600
MAX_CHANNELS = 20
CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"


class ConfigError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoogleProjectCfg(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,29}$")
    daily_quota_units: int = Field(default=10_000, gt=0)
    oauth_client_ref: str = Field(
        default="vault://youtube-oauth-client", pattern=r"^(vault|env)://"
    )


class ClusterCfg(_Strict):
    id: str
    display_name: str
    niche: str = ""
    # Rollout: a cluster that requires another stays dark until that one has run live for
    # gate_days and an admin signed it off (enforced by the database).
    requires_cluster: str | None = None
    live: bool = False
    gate_days: int = Field(default=14, ge=1, le=90)
    slot_spacing_minutes: int = Field(default=30, ge=0, le=360)
    max_concurrent_jobs: int = Field(default=4, ge=1, le=50)
    daily_budget_cents: int | None = Field(default=None, ge=0)
    google_projects: list[GoogleProjectCfg] = Field(min_length=1)
    pools: dict[Literal["research", "script_seo", "media", "gpu_render", "qa"], int] = Field(
        default_factory=dict
    )

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError("id must be a lowercase slug")
        return v

    @field_validator("pools")
    @classmethod
    def _positive(cls, v: dict[str, int]) -> dict[str, int]:
        if any(n < 1 for n in v.values()):
            raise ValueError("pool limits must be >= 1")
        return v


class PublishCfg(_Strict):
    hour_local: int = Field(default=17, ge=0, le=23)
    window_minutes: int = Field(default=0, ge=0, le=720)
    approval_lead_minutes: int = Field(default=60, ge=5, le=4320)


class BudgetCfg(_Strict):
    job_cents: int = Field(default=1000, ge=0)
    daily_cents: int = Field(default=10_000, ge=0)


class BrandCfg(_Strict):
    brand_voice: str | None = None
    hook_library: str | None = None
    banned_claims: str | None = None
    style_guide: str | None = None
    offers: str | None = None
    playbook: str | None = None


class PolicyCfg(_Strict):
    # Production policy (min sources, tone and forbidden words ...) and the safety policy.
    production: dict[str, Any] = Field(default_factory=dict)
    safety: SafetyPolicy = Field(default_factory=SafetyPolicy)


class ChannelCfg(_Strict):
    slug: str
    display_name: str
    cluster: str
    google_project: str | None = None
    autonomy_tier: Literal["shadow", "gated"] = "shadow"
    audience_timezone: str = "UTC"
    publish: PublishCfg = Field(default_factory=PublishCfg)
    budget: BudgetCfg = Field(default_factory=BudgetCfg)
    brand: BrandCfg = Field(default_factory=BrandCfg)
    policy: PolicyCfg = Field(default_factory=PolicyCfg)
    pools: dict[Literal["research", "script_seo", "media", "gpu_render", "qa"], int] = Field(
        default_factory=dict
    )
    uploads_per_day: int = Field(default=1, ge=1, le=6)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError("slug must be a lowercase slug (a-z, 0-9, - and _)")
        return v

    @field_validator("audience_timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {v!r}") from None
        return v

    @model_validator(mode="after")
    def _budgets(self) -> ChannelCfg:
        if self.budget.job_cents > self.budget.daily_cents:
            raise ValueError("budget.job_cents cannot exceed budget.daily_cents")
        return self

    # Derived references: always vault references, never values. They match the secret ids that
    # infra/terraform creates for each channel.
    @property
    def youtube_credentials_ref(self) -> str:
        return f"vault://youtube-refresh-token-{self.slug}"

    @property
    def analytics_credentials_ref(self) -> str:
        return f"vault://youtube-analytics-token-{self.slug}"

    def knowledge(self) -> dict[KnowledgeKind, str]:
        out: dict[KnowledgeKind, str] = {}
        for kind in KnowledgeKind:
            if kind is KnowledgeKind.POLICY:
                continue
            text = getattr(self.brand, kind.value, None)
            if text and text.strip():
                out[kind] = text.strip() + "\n"
        policy = dict(self.policy.production)
        policy["safety"] = json.loads(self.policy.safety.model_dump_json())
        out[KnowledgeKind.POLICY] = json.dumps(policy, sort_keys=True, indent=2) + "\n"
        return out


class TargetsCfg(_Strict):
    """Service targets the load simulation (and operators) hold the platform to."""

    min_success_rate: float = Field(default=0.97, ge=0, le=1)
    max_cost_per_video_cents: int = Field(default=100, ge=1)
    max_approval_queue_depth: int = Field(default=25, ge=1)
    max_sim_day_seconds: int = Field(default=120, ge=1)


class PlatformCfg(_Strict):
    # Global (fleet-wide) fair-share limits per shared pool, e.g. the GPU render queue.
    pools: dict[Literal["research", "script_seo", "media", "gpu_render", "qa"], int] = Field(
        default_factory=dict
    )
    targets: TargetsCfg = Field(default_factory=TargetsCfg)


class FleetCfg(_Strict):
    clusters: dict[str, ClusterCfg]
    channels: dict[str, ChannelCfg]
    platform: PlatformCfg = Field(default_factory=PlatformCfg)

    def project_of(self, channel: ChannelCfg) -> GoogleProjectCfg:
        cluster = self.clusters[channel.cluster]
        if channel.google_project is None:
            return cluster.google_projects[0]
        return next(p for p in cluster.google_projects if p.id == channel.google_project)


def _scan_secrets(value: Any, where: str) -> None:
    if isinstance(value, str):
        try:
            ensure_no_secrets(value, where)
        except SecretInInput as exc:
            raise ConfigError(str(exc)) from None
    elif isinstance(value, dict):
        for k, v in value.items():
            _scan_secrets(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _scan_secrets(v, f"{where}[{i}]")


def _read(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: invalid YAML ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: must be a mapping")
    _scan_secrets(data, path.name)
    return data


def validate_fleet(fleet: FleetCfg, *, max_channels: int = MAX_CHANNELS) -> None:
    """Cross-file rules. Raises ConfigError with every problem found."""
    problems: list[str] = []
    if len(fleet.channels) > max_channels:
        problems.append(f"{len(fleet.channels)} channels exceed the limit of {max_channels}")
    for cid, cluster in fleet.clusters.items():
        if cluster.requires_cluster and cluster.requires_cluster not in fleet.clusters:
            problems.append(f"cluster {cid} requires unknown cluster {cluster.requires_cluster}")
        # no cycles
        seen, cur = {cid}, cluster.requires_cluster
        while cur and cur in fleet.clusters:
            if cur in seen:
                problems.append(f"cluster {cid} has a rollout cycle")
                break
            seen.add(cur)
            cur = fleet.clusters[cur].requires_cluster
        if cluster.requires_cluster and cluster.live:
            problems.append(
                f"cluster {cid} requires {cluster.requires_cluster}: live must be false"
            )
    project_ids = [p.id for c in fleet.clusters.values() for p in c.google_projects]
    for dup in {p for p in project_ids if project_ids.count(p) > 1}:
        problems.append(f"google project {dup} is declared in more than one cluster")
    used: dict[str, int] = {}
    for slug, ch in fleet.channels.items():
        if ch.cluster not in fleet.clusters:
            problems.append(f"channel {slug} is in unknown cluster {ch.cluster}")
            continue
        cluster = fleet.clusters[ch.cluster]
        if ch.google_project and ch.google_project not in {p.id for p in cluster.google_projects}:
            problems.append(f"channel {slug}: project {ch.google_project} is not in {ch.cluster}")
            continue
        if not cluster.live and ch.autonomy_tier == "gated":
            problems.append(f"channel {slug} is gated but cluster {ch.cluster} is not live yet")
        project = fleet.project_of(ch)
        used[project.id] = used.get(project.id, 0) + ch.uploads_per_day
    # Quota arithmetic: leave 20% headroom for status checks and retries.
    quotas = {p.id: p.daily_quota_units for c in fleet.clusters.values() for p in c.google_projects}
    for pid, uploads in used.items():
        if uploads * UPLOAD_QUOTA_UNITS > quotas[pid] * 0.8:
            problems.append(
                f"google project {pid}: {uploads} uploads/day need {uploads * UPLOAD_QUOTA_UNITS}"
                f" units but only {int(quotas[pid] * 0.8)} (80% of quota) are usable"
            )
    if problems:
        raise ConfigError("; ".join(problems))


def load_fleet(root: Path | None = None, *, max_channels: int = MAX_CHANNELS) -> FleetCfg:
    root = root or CONFIG_ROOT
    clusters: dict[str, ClusterCfg] = {}
    channels: dict[str, ChannelCfg] = {}
    for path in sorted((root / "clusters").glob("*.yaml")):
        cfg = ClusterCfg.model_validate(_read(path))
        if path.stem != cfg.id:
            raise ConfigError(f"{path.name}: file name must be {cfg.id}.yaml")
        if cfg.id in clusters:
            raise ConfigError(f"duplicate cluster {cfg.id}")
        clusters[cfg.id] = cfg
    for path in sorted((root / "channels").glob("*.yaml")):
        ch = ChannelCfg.model_validate(_read(path))
        if path.stem != ch.slug:
            raise ConfigError(f"{path.name}: file name must be {ch.slug}.yaml")
        if ch.slug in channels:
            raise ConfigError(f"duplicate channel {ch.slug}")
        channels[ch.slug] = ch
    platform = PlatformCfg()
    if (root / "platform.yaml").is_file():
        platform = PlatformCfg.model_validate(_read(root / "platform.yaml"))
    fleet = FleetCfg(clusters=clusters, channels=channels, platform=platform)
    validate_fleet(fleet, max_channels=max_channels)
    return fleet


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m ytcore.channels.config")
    parser.add_argument("cmd", choices=["validate", "tfvars"])
    parser.add_argument("--dir", type=Path, default=CONFIG_ROOT)
    args = parser.parse_args(argv)
    try:
        fleet = load_fleet(args.dir)
    except (ConfigError, ValueError) as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 1
    if args.cmd == "tfvars":
        print(json.dumps({"channel_ids": sorted(fleet.channels)}))
    else:
        print(f"ok: {len(fleet.clusters)} cluster(s), {len(fleet.channels)} channel(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())

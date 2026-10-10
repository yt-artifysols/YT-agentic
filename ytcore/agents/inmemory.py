"""In-memory ProductionStore for dev, tests and evals (mirrors the database rules)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ytcore.agents.context import ChannelProfile
from ytcore.agents.costs import Spend
from ytcore.agents.fake import FAKE_MUSIC_LICENSE, FAKE_STOCK_LICENSE
from ytcore.store.errors import BudgetExceeded, JobBudgetExceeded, LicenseInvalid, StoreError


@dataclass
class License:
    ref: str
    kind: str
    channel_id: str | None = None
    expires_at: datetime | None = None
    revoked: bool = False


@dataclass
class _Channel:
    profile: ChannelProfile
    job_budget_cents: int
    daily_budget_cents: int


@dataclass
class InMemoryProductionStore:
    channels: dict[str, _Channel] = field(default_factory=dict)
    job_channel: dict[str, str] = field(default_factory=dict)
    costs: list[tuple[str, str, str, int, str, dict[str, Any]]] = field(default_factory=list)
    alerts: list[tuple[str, str | None, str, str]] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)
    licenses: dict[str, License] = field(default_factory=dict)
    qa_notes: dict[str, list[str]] = field(default_factory=dict)
    rejections: dict[str, str] = field(default_factory=dict)
    risks: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def add_channel(
        self,
        channel_id: str,
        *,
        autonomy_tier: str = "shadow",
        job_budget_cents: int = 1000,
        daily_budget_cents: int = 10_000,
        fake_licenses: bool = True,
    ) -> ChannelProfile:
        profile = ChannelProfile(
            channel_id, f"channels/{channel_id}/", f"mem:{channel_id}", autonomy_tier
        )
        self.channels[channel_id] = _Channel(profile, job_budget_cents, daily_budget_cents)
        if fake_licenses:
            self.licenses.setdefault(FAKE_MUSIC_LICENSE, License(FAKE_MUSIC_LICENSE, "music"))
            self.licenses.setdefault(FAKE_STOCK_LICENSE, License(FAKE_STOCK_LICENSE, "stock"))
        return profile

    def add_job(self, job_id: str, channel_id: str) -> None:
        self.job_channel[job_id] = channel_id

    # ChannelDirectory
    def channel_profile(self, channel_id: str) -> ChannelProfile:
        if channel_id not in self.channels:
            raise StoreError(f"unknown channel {channel_id}")
        return self.channels[channel_id].profile

    def _channel_of(self, job_id: str) -> _Channel:
        return self.channels[self.job_channel[job_id]]

    # CostLedger
    def spend(self, job_id: str, channel_id: str) -> Spend:
        ch = self.channels[channel_id]
        today = datetime.now(UTC).date().isoformat()
        return Spend(
            job_cents=sum(c[3] for c in self.costs if c[0] == job_id),
            job_budget_cents=ch.job_budget_cents,
            channel_today_cents=sum(
                c[3] for c in self.costs if c[1] == channel_id and c[5].get("day", today) == today
            ),
            channel_budget_cents=ch.daily_budget_cents,
        )

    def record_cost(
        self,
        job_id: str,
        kind: str,
        cost_cents: int,
        *,
        provider: str,
        detail: dict[str, Any],
        actor: str,
        over_cap: bool = False,
    ) -> None:
        channel_id = self.job_channel[job_id]
        s = self.spend(job_id, channel_id)
        if over_cap:
            self.costs.append(
                (job_id, channel_id, kind, cost_cents, provider, {**detail, "over_cap": True})
            )
            return
        if s.channel_today_cents + cost_cents > s.channel_budget_cents:
            raise BudgetExceeded(f"daily budget exceeded for channel {channel_id}")
        if s.job_cents + cost_cents > s.job_budget_cents:
            raise JobBudgetExceeded(f"job budget exceeded for job {job_id}")
        self.costs.append((job_id, channel_id, kind, cost_cents, provider, dict(detail)))

    def alert(self, channel_id: str, job_id: str | None, kind: str, message: str) -> None:
        self.alerts.append((channel_id, job_id, kind, message))

    # ProductionStore
    def license_valid(self, license_ref: str, channel_id: str) -> bool:
        lic = self.licenses.get(license_ref)
        return bool(
            lic
            and not lic.revoked
            and lic.channel_id in (None, channel_id)
            and (lic.expires_at is None or lic.expires_at > datetime.now(UTC))
        )

    def record_asset(
        self,
        job_id: str,
        channel_id: str,
        *,
        name: str,
        kind: str,
        sha256: str,
        media_type: str,
        provider: str,
        license_ref: str | None,
    ) -> int:
        if self.job_channel.get(job_id) != channel_id:
            raise StoreError("job belongs to another channel")
        if kind in ("music", "stock") and not license_ref:
            raise LicenseInvalid(f"{kind} assets need a license_ref")
        if license_ref and not self.license_valid(license_ref, channel_id):
            raise LicenseInvalid(f"license {license_ref} is not valid for channel {channel_id}")
        version = 1 + sum(1 for a in self.assets if a["job_id"] == job_id and a["name"] == name)
        self.assets.append(
            {
                "job_id": job_id,
                "channel_id": channel_id,
                "name": name,
                "kind": kind,
                "sha256": sha256,
                "version": version,
                "license_ref": license_ref,
                "provider": provider,
            }
        )
        return version

    def record_risk(self, job_id: str, channel_id: str, assessment: dict[str, Any]) -> None:
        self.risks.append((job_id, channel_id, dict(assessment)))

    def revision_notes(self, job_id: str) -> list[str]:
        notes = list(self.qa_notes.get(job_id, []))
        if job_id in self.rejections:
            notes.append(f"reviewer: {self.rejections[job_id]}")
        return notes

    def record_qa_flags(self, job_id: str, notes: list[str]) -> None:
        self.qa_notes[job_id] = list(notes)

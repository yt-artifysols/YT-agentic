"""What the agent pipeline needs from storage, beyond the workflow Backend."""

from __future__ import annotations

from typing import Any, Protocol

from ytcore.agents.context import ChannelDirectory
from ytcore.agents.costs import CostLedger
from ytcore.gateway.assets import AssetSink, AssetSource


class ProductionStore(ChannelDirectory, CostLedger, Protocol):
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
        """Append a versioned asset row; returns its version. Refuses unlicensed music/stock."""
        ...

    def license_valid(self, license_ref: str, channel_id: str) -> bool: ...

    def record_risk(self, job_id: str, channel_id: str, assessment: dict[str, Any]) -> None:
        """Store the risk assessment (score 0..1 and named reasons) on the job."""
        ...

    def revision_notes(self, job_id: str) -> list[str]:
        """Notes for a rework: blocking QA findings and the latest human rejection reason."""
        ...

    def record_qa_flags(self, job_id: str, notes: list[str]) -> None:
        """Remember blocking QA notes for the next rework (no-op where qa_reports is the source)."""
        ...


class AssetStore(AssetSink, AssetSource, Protocol):
    pass

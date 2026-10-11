"""Read-only YouTube Analytics access. Separate from the upload gateway: it uses a per-channel
read-only OAuth token (`channels.analytics_credentials_ref`), never the upload token."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from ytcore.secrets.provider import SecretValue


@dataclass(frozen=True)
class VideoMetricsRow:
    youtube_video_id: str
    metric_date: date
    views: int
    impressions: int = 0
    ctr: float | None = None
    avg_view_seconds: float | None = None
    retention: float | None = None
    revenue_cents: int | None = None  # None when the channel is not monetised / scope missing


class AnalyticsClient(Protocol):
    is_fake: bool

    def fetch(self, *, token: SecretValue, video_id: str, day: date) -> VideoMetricsRow | None: ...


@dataclass
class FakeAnalyticsClient:
    """Deterministic in-memory metrics: {video_id: {date: row}}."""

    data: dict[str, dict[date, VideoMetricsRow]] = field(default_factory=dict)
    is_fake: bool = True
    calls: int = 0

    def fetch(self, *, token: SecretValue, video_id: str, day: date) -> VideoMetricsRow | None:
        self.calls += 1
        if not token.reveal():
            raise RuntimeError("missing analytics credentials")
        return self.data.get(video_id, {}).get(day)


class RealYouTubeAnalyticsClient:  # pragma: no cover - needs real OAuth credentials
    """YouTube Analytics API v2 reports.query. Production only (`youtube` extra)."""

    is_fake = False

    def __init__(self, oauth_client: SecretValue) -> None:
        self._oauth_client = oauth_client

    def fetch(self, *, token: SecretValue, video_id: str, day: date) -> VideoMetricsRow | None:
        import json

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        client = json.loads(self._oauth_client.reveal())
        creds = Credentials(  # type: ignore[no-untyped-call]
            None,
            refresh_token=token.reveal(),
            token_uri="https://oauth2.googleapis.com/token",  # noqa: S106 - public endpoint
            client_id=client["client_id"],
            client_secret=client["client_secret"],
            scopes=["https://www.googleapis.com/auth/yt-analytics.readonly"],
        )
        api: Any = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
        resp = (
            api.reports()
            .query(
                ids="channel==MINE",
                startDate=day.isoformat(),
                endDate=day.isoformat(),
                metrics="views,averageViewDuration,averageViewPercentage,"
                "impressions,impressionClickThroughRate",
                filters=f"video=={video_id}",
            )
            .execute()
        )
        rows = resp.get("rows") or []
        if not rows:
            return None
        views, avg_dur, avg_pct, impressions, ctr = rows[0][:5]
        return VideoMetricsRow(
            youtube_video_id=video_id,
            metric_date=day,
            views=int(views),
            impressions=int(impressions),
            ctr=float(ctr) / 100 if ctr is not None else None,
            avg_view_seconds=float(avg_dur),
            retention=float(avg_pct) / 100,
            revenue_cents=None,  # revenue needs the monetary scope: linked per channel manually
        )

"""`ytctl channel onboard <slug>`: apply the channel's config and print the onboarding checklist.

The checklist never prints secret values: it only says whether a secret reference resolves.
Steps that need a person (OAuth consent, approving knowledge, reviewing the dry run) are listed
with exactly what to do next.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import psycopg

from ytcore.channels.apply import ApplyReport, apply_fleet
from ytcore.channels.config import ChannelCfg, FleetCfg
from ytcore.secrets.provider import SecretRef, SecretsProvider
from ytcore.store.postgres import Connect

Status = Literal["done", "todo", "blocked"]

OAUTH_SCOPES = ("https://www.googleapis.com/auth/youtube.upload",)
ANALYTICS_SCOPES = ("https://www.googleapis.com/auth/yt-analytics.readonly",)


@dataclass(frozen=True)
class Item:
    status: Status
    title: str
    detail: str = ""


def subset(fleet: FleetCfg, slug: str) -> FleetCfg:
    """Only the channel and the clusters it depends on (so onboarding one channel never touches
    the others)."""
    channel = fleet.channels[slug]
    clusters = {}
    cur: str | None = channel.cluster
    while cur is not None:
        clusters[cur] = fleet.clusters[cur]
        cur = fleet.clusters[cur].requires_cluster
    return FleetCfg(clusters=clusters, channels={slug: channel}, platform=fleet.platform)


def _secret_present(secrets: SecretsProvider, ref: str) -> bool:
    try:
        return bool(secrets.get(SecretRef(ref)).reveal())
    except Exception:  # missing, denied, unconfigured: all mean "not usable yet"
        return False


def oauth_instructions(ch: ChannelCfg) -> list[str]:
    return [
        f"1. In the Google Cloud project for {ch.slug}, make sure the OAuth consent screen lists "
        f"the scope {OAUTH_SCOPES[0]}.",
        f"2. Sign in to the YouTube channel '{ch.display_name}' as its owner and complete the "
        "consent flow for the upload scope (a browser step only the channel owner can do).",
        f"3. Put the resulting REFRESH TOKEN into Secret Manager as a new version of "
        f"'youtube-refresh-token-{ch.slug}' (console or gcloud; never paste it in chat, a ticket "
        "or this repo).",
        f"4. Repeat once with the read-only scope {ANALYTICS_SCOPES[0]} for "
        f"'youtube-analytics-token-{ch.slug}' (a separate token the gateway can never read).",
        f"5. Add '{ch.slug}' to channel_ids in infra/terraform/envs/<env> so the secret containers "
        "and IAM exist (python -m ytcore.channels.config tfvars prints the list).",
    ]


def checklist(
    connect: Connect,
    fleet: FleetCfg,
    slug: str,
    secrets: SecretsProvider,
    report: ApplyReport,
) -> list[Item]:
    ch = fleet.channels[slug]
    cluster = fleet.clusters[ch.cluster]
    items = [
        Item("done", "Configuration valid", f"config/channels/{slug}.yaml and its cluster"),
        Item(
            "done",
            "Applied to the database",
            f"channel {report.channels.get(slug, '?')}, "
            f"cluster {report.clusters.get(ch.cluster, '?')}",
        ),
    ]
    drafts = report.drafts.get(slug, {})
    pending = sorted(k for k, v in drafts.items() if v in {"proposed", "pending"})
    items.append(
        Item(
            "todo" if pending else "done",
            "Brand and policy approved by a human",
            ("approve in the hub: " + ", ".join(pending)) if pending else "all documents approved",
        )
    )
    for label, ref in (
        ("YouTube upload token", ch.youtube_credentials_ref),
        ("YouTube analytics token", ch.analytics_credentials_ref),
    ):
        ok = _secret_present(secrets, ref)
        items.append(Item("done" if ok else "todo", f"{label} stored", ref))
    items.append(
        Item(
            "done" if _secret_present(secrets, fleet.project_of(ch).oauth_client_ref) else "todo",
            "OAuth client stored",
            fleet.project_of(ch).oauth_client_ref,
        )
    )
    live = _cluster_live(connect, ch.cluster)
    items.append(
        Item(
            "done" if live else "blocked",
            f"Cluster {ch.cluster} is live",
            "publishing allowed"
            if live
            else f"dark until {cluster.requires_cluster or 'enabled'} passes its rollout gate",
        )
    )
    items.append(
        Item(
            "done" if ch.autonomy_tier == "gated" else "todo",
            "Shadow dry run reviewed, then tier set to gated",
            f"autonomy_tier is {ch.autonomy_tier}: run "
            f"`ytctl channel onboard {slug} --dry-run-jobs 3`"
            " and review the videos in the hub; change the tier in the YAML (reviewed PR)",
        )
    )
    return items


def _cluster_live(connect: Connect, cluster_id: str) -> bool:
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT live_enabled FROM clusters WHERE id = %s", (cluster_id,)
            ).fetchone()
    except psycopg.Error:
        return False
    return bool(row and row[0])


def onboard(
    connect: Connect, fleet: FleetCfg, slug: str, secrets: SecretsProvider, *, dry_run: bool = False
) -> tuple[ApplyReport, list[Item]]:
    if slug not in fleet.channels:
        raise KeyError(f"no config/channels/{slug}.yaml")
    report = apply_fleet(connect, subset(fleet, slug), dry_run=dry_run)
    return report, checklist(connect, fleet, slug, secrets, report)


def render(items: list[Item]) -> str:
    mark = {"done": "[x]", "todo": "[ ]", "blocked": "[!]"}
    return "\n".join(
        f"{mark[i.status]} {i.title}" + (f"\n      {i.detail}" if i.detail else "") for i in items
    )

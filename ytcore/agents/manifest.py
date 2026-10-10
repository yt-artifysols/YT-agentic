"""Manifest builder: the canonical publish package whose content hash the approval binds to.

The package carries the publishable metadata, the sha256 of every asset (including the research
and script documents), per-asset details (kind, version, license) and provenance: the approved
knowledge versions, the owner inputs used and the prompt/model versions. All of it is covered
by the content hash, so an approval is for exactly this provenance too.
"""

from __future__ import annotations

from typing import Any

from ytcore.agents.context import ChannelPolicy
from ytcore.agents.models import MediaOutput, QAOutput, ResearchOutput, ScriptOutput
from ytcore.approvals.hashing import content_hash
from ytcore.knowledge.models import KnowledgeSnapshot
from ytcore.safety.checks import ensure_disclosed

PIPELINE_VERSION = "agents-v1"


def build_manifest(
    *,
    job_id: str,
    channel_id: str,
    research: ResearchOutput,
    script: ScriptOutput,
    media: MediaOutput,
    documents: dict[str, str],
    knowledge: KnowledgeSnapshot,
    policy: ChannelPolicy,
    qa: QAOutput | None = None,
) -> dict[str, Any]:
    if knowledge.channel_id != channel_id or research.brief.channel_id != channel_id:
        raise ValueError("manifest inputs belong to another channel")
    assets = {a.name: a.sha256 for a in media.assets} | documents
    manifest: dict[str, Any] = {
        "metadata": {
            "job_id": job_id,
            "channel_id": channel_id,
            "title": script.title,
            "description": script.description,
            "tags": list(script.tags),
            "script": script.full_text(),
            "category_id": policy.category_id,
            "default_language": policy.language,
            "privacy_status": "private",
            "made_for_kids": policy.made_for_kids,
            # AI-produced content is always disclosed as synthetic.
            "contains_synthetic_media": True,
        },
        "assets": assets,
        "asset_info": {
            a.name: {
                "kind": a.kind,
                "version": a.version,
                "media_type": a.media_type,
                "provider": a.provider,
                "license_ref": a.license_ref,
            }
            for a in media.assets
        },
        "provenance": {
            "pipeline": PIPELINE_VERSION,
            "knowledge": knowledge.versions(),
            "knowledge_docs": knowledge.doc_ids(),
            "owner_inputs": sorted(research.brief.owner_input_ids),
            "goals": sorted(research.brief.goal_input_ids),
            "sources": [{"id": s.id, "url": s.url, "sha256": s.sha256} for s in research.sources],
            "prompts": {
                "research": research.prompt_version,
                "script_seo": script.prompt_version,
                "media": media.prompt_version,
                **({"qa": qa.prompt_version} if qa else {}),
            },
            "models": sorted({research.model, script.model, media.model}),
        },
    }
    content_hash(manifest)  # validates every asset digest
    ensure_disclosed(manifest["asset_info"], manifest["metadata"]["contains_synthetic_media"])
    return manifest

# ADR 0007: Knowledge base, owner inputs and agent pools v1

- Status: Accepted
- Date: 2026-10-10

## Context
Agents must produce on-brand, accurate videos per channel from human-owned guidance and owner
inputs, without ever being able to change that guidance, publish, or be steered by scraped text.

## Decision
- **Knowledge** (`knowledge_docs`, migration 006): versioned per channel and kind. Anyone,
  agents included, submits drafts; only an active hub user with the approver role can approve
  or reject (DB trigger), in the approval hub only. Approved versions are immutable and
  append-only; a draft based on an older version is refused as stale. Each job pins the latest
  approved versions when research starts (`job_knowledge`) and every later stage uses the pins.
- **Owner inputs** (`owner_inputs`): ideas, footage, notes and goals, deduplicated by sha256.
  The brief generator claims the oldest new inputs first (`job_owner_inputs`, marks them used);
  goals stay as standing context. Knowledge versions and owner input ids are in the manifest,
  so the approval's content hash covers the provenance.
- **Four pools** (`ytcore/agents`): research, script_seo, media, qa. Specialist roles are skills
  inside them. Typed pydantic IO, versioned prompt files, provider protocols with fakes and a
  Claude adapter, explicit tool allowlists (research: read-only web fetch; script: none; media:
  providers and storage; qa: read-only storage and the rights register).
- **Untrusted content** goes to models only inside unforgeable data blocks; models get no tools
  and replies are parsed strictly.
- **Costs** are authorised before and recorded after every provider call; per-job (YTB02) and
  per-channel daily (YTB01) caps fail the job with a reason and an alert.
- **Isolation**: every repository is bound to one channel; memory rows carry the channel's own
  namespace (composite FK); cross-channel links are impossible by foreign key.
- **Shadow mode** is the existing `shadow` autonomy tier (tier 0); the DB refuses scheduling
  and publishing for it.
- **Evals** (`ytcore/evals`) gate prompt and model changes in CI against committed baselines.

## Consequences
- Fake providers make CI deterministic but say little about real quality; real-model baselines
  must be recorded per model (`baselines/<agent>.<model>.json`) once providers are chosen.
- Production refuses fake media providers until real adapters exist.

# Policy sources

The deterministic rules in `ytcore/safety/` are derived from the official YouTube policy pages
below, fetched on **2026-10-10**. Most pages show no version or revision date (only "©2026
Google"); the only dated entry is the monetization page (July 15, 2025). Re-fetch and update this
file, the rule table and the red-team tests when YouTube changes a policy. **A human must review
the rules** (see "Manual steps" in the Phase 3 report): this is an engineering reading of the
pages, not legal advice, and a passing check never means a video is compliant.

| Policy page | URL | Version shown | What we encode |
|---|---|---|---|
| Spam policy (spam, deceptive practices, scams) | https://support.google.com/youtube/answer/2801973 | none (©2026 Google) | `misleading_title`, `misleading_thumbnail`: "maliciously misleading titles, thumbnails, descriptions or imagery" that promise something the video does not deliver; applies to thumbnails and all metadata. "Get rich quick" schemes and fake support/job offers: `scam_pattern` rule. |
| YouTube channel monetization policies | https://support.google.com/youtube/answer/1311392 | latest entry **July 15, 2025** ("repetitious content" renamed "inauthentic content") | `repetitive_content`, `mass_produced_content`, `reused_content`: interchangeable/templated videos, AI content built on generic templates, reposted material without significant original commentary or added value. Allowed: a consistent intro/outro with different main content, series with distinct storylines. `ai_expert_persona`: AI personas presenting as human experts on health, legal, finance or politics. |
| Disclosing use of GenAI content | https://support.google.com/youtube/answer/14328491 | none (©2026 Google) | `missing_synthetic_disclosure`: disclosure is required for realistic content that makes a real person appear to say or do something they did not, altered footage of a real event or place, and realistic scenes that never happened. Not needed for clearly non-realistic/animated content, minor aesthetic edits, cloning your own voice for voiceover, or AI used only for scripts, titles, thumbnails and ideas. Disclosure is set at upload ("AI use" question in Studio); repeated non-disclosure can bring labels or YPP penalties. We always set `contains_synthetic_media` (the pipeline produces synthetic voice and video), which is the conservative choice. |
| Medical misinformation policy | https://support.google.com/youtube/answer/13813322 | none (©2026 Google) | Sensitive topic `health` and the `medical_claim` rule: prevention and treatment claims that contradict health-authority guidance (bleach/gasoline as prevention, guaranteed cancer cures, discouraging approved treatment, vaccine claims, alternative formula). Banned-claim lists per channel carry the specifics. |
| Child safety policy | https://support.google.com/youtube/answer/2801999 | none (©2026 Google) | Sensitive topic `minors`: minors in dangerous acts, abuse or distress, misleading family content, doxxing/bullying of minors. Default handling is `block` for stated categories and `escalate` otherwise. Anyone under 18 is a minor. |
| Misinformation policies | https://support.google.com/youtube/answer/10834785 | none (©2026 Google) | Sensitive topics `politics` and `breaking_news`: manipulated content, misattributed (old footage as current events), census suppression, harmful misinformation. **The separate "Elections misinformation policies" page was not fetched**; politics is therefore handled as "escalate to a human" and not encoded as automatic rules. |

## What is deliberately not automated

- Copyright and Content ID matching: only license presence/validity from the rights register and
  a per-channel blocklist of media hashes. Fingerprint matching needs an external service.
- Whether a given video is "realistic" synthetic media: we disclose always.
- Elections policy details, advertiser-friendly (monetization) suitability, and regional law.
- Any claim that content "will not be demonetized or removed".

## How the rules map to code

| Rule code | Severity | Where |
|---|---|---|
| `banned_topic` | block | `ytcore/safety/checks.py` (per-channel `safety.banned_topics`) |
| `sensitive_topic` | warn or block | `checks.py` + `lexicons.py` (per-channel `safety.sensitive`) |
| `missing_synthetic_disclosure` | block | `checks.py` |
| `missing_license` / `invalid_license` | block | `checks.py` |
| `repetitive_content`, `mass_produced_content` | warn / block | `checks.py`, `duplicates.py` |
| `misleading_title`, `misleading_thumbnail` | warn / block | `checks.py` (LLM only for borderline cases) |
| `scam_pattern`, `ai_expert_persona`, `medical_claim` | block | `checks.py` |
| `duplicate_script`, `reused_media`, `repeated_title_template`, `repeated_thumbnail_text` | escalate / block | `duplicates.py` |

// Typed client for the approval hub API (mirrors ytcore/approvals/service/openapi.json).
import { startAuthentication } from "@simplewebauthn/browser";
import { DEMO, demoRequest } from "./demo";

export type Role = "approver" | "operator" | "admin";

export interface Me {
  username: string;
  display_name: string;
  role: Role;
  csrf_token?: string | null;
}

export interface QueueItem {
  job_id: string;
  channel_id: string;
  cluster_id: string | null;
  title: string;
  origin: string;
  content_hash: string;
  manifest_version: number;
  risk_score: number | null;
  qa_passed: boolean | null;
  flag_count: number;
  approval_deadline: string | null;
  fast_lane: boolean;
  lane: "standard" | "fast" | "deep";
  lane_reason: string;
}

export interface QA {
  passed: boolean;
  risk_score: number;
  flags: Record<string, unknown>[];
  sources: Record<string, unknown>[];
  for_current_content: boolean;
}

export interface Preview {
  job_id: string;
  channel_id: string;
  state: string;
  content_hash: string;
  manifest_version: number;
  title: string;
  description: string;
  tags: string[];
  script: string;
  metadata: Record<string, unknown>;
  assets: { name: string; digest: string; url: string }[];
  video_url: string | null;
  thumbnail_url: string | null;
  qa: QA | null;
  fast_lane: boolean;
  lane: "standard" | "fast" | "deep";
  lane_reason: string;
  previous: { version: number; metadata: Record<string, unknown> } | null;
}

export interface Controls {
  global_paused: boolean;
  clusters: { id: string; name: string; status: string }[];
  channels: { id: string; name: string; cluster_id: string | null; status: string; tier: string }[];
}

export interface ClusterRow {
  id: string;
  display_name: string;
  niche: string;
  status: string;
  live_enabled: boolean;
  live_since: string | null;
  requires_cluster: string | null;
  gate_days: number;
  max_concurrent_jobs: number;
  channels: number;
  awaiting_approval: number;
  oldest_waiting_since: string | null;
  health: Record<string, unknown> | null;
}

export interface Scorecard {
  channel_id: string;
  cluster_id: string | null;
  score_date: string;
  jobs_planned: number;
  jobs_approved: number;
  jobs_rejected: number;
  approval_rate: number | null;
  policy_flags: number;
  videos_published: number;
  cost_cents: number;
  cost_per_video: number | null;
  avg_ctr: number | null;
  avg_retention: number | null;
  views: number;
  revenue_cents: number | null;
}

export interface Gate {
  cluster_id: string;
  live: boolean;
  live_days: number;
  required_days: number;
  breaker_trips: number;
  stuck_or_plan_alerts: number;
  failed_uploads: number;
  pending_uploads: number;
  decisions: number;
  rejection_rate: number | null;
  scorecard_days: number;
  problems: string[];
  ok: boolean;
}

export interface TierRow {
  channel_id: string;
  name: string;
  cluster_id: string | null;
  tier: number;
  tier_since: string;
  last_change_kind: string | null;
  last_change_reason: string | null;
  last_change_at: string | null;
  recommendation_id: number | null;
  recommendation_evidence: Record<string, unknown> | null;
}

export interface TierChange {
  id: number;
  channel_id: string;
  from_tier: number;
  to_tier: number;
  kind: "promotion" | "demotion";
  reason: string;
  actor: string;
  evidence: Record<string, unknown>;
  recommendation_id: number | null;
  created_at: string;
}

export interface TierReport {
  spot_checks: { n: number; disagreements: number; rate: number | null };
  low_risk_all: { n: number; disagreements: number; rate: number | null };
  by_risk_bucket: Record<string, { n: number; not_approved: number }>;
}

export interface Overview {
  generated_at: string;
  awaiting_approval: { count: number; oldest_deadline: string | null; due_within_2h: number };
  in_production: number;
  last_24h: { live: number; failed: number; cancelled: number; rejected: number };
  kill_switch: { global_paused: boolean; paused_clusters: number; paused_channels: number };
  cost_today_cents: number;
  daily_budget_cents: number;
  alerts_24h: number;
  alerts: AlertRow[];
  activity: { id: number; job_id: string; channel_id: string; from_state: string | null; to_state: string; actor: string; at: string; title: string }[];
  clusters: { id: string; name: string; status: string; live_enabled: boolean; channels: number; awaiting_approval: number }[];
  tiers: { pending_recommendations: number; demotions_7d: number };
  channels: number;
}

export interface AlertRow {
  id: number;
  kind: string;
  message: string;
  channel_id: string | null;
  cluster_id: string | null;
  job_id?: string | null;
  created_at: string;
}

export interface PipelineItem {
  job_id: string;
  channel_id: string;
  cluster_id: string | null;
  state: string;
  origin: string;
  title: string;
  created_at: string;
  updated_at: string;
  approval_deadline: string | null;
  risk_score: number | null;
  youtube_video_id: string | null;
}

export interface Pipeline {
  items: PipelineItem[];
  counts: Record<string, number>;
}

export interface TimelineEvent {
  id: number;
  from_state: string | null;
  to_state: string;
  actor: string;
  at: string;
  payload: Record<string, unknown>;
}

export interface ChannelRow {
  id: string;
  name: string;
  cluster_id: string | null;
  status: string;
  autonomy: string;
  approval_tier: number;
  daily_budget_cents: number;
  timezone: string;
  awaiting_approval: number;
  in_production: number;
  published_7d: number;
  next_slot: string | null;
  approval_rate_30d: number | null;
}

export interface ChannelDetail extends ChannelRow {
  knowledge: { kind: string; version: number; status: string; proposed_at: string | null }[];
  recent_jobs: { job_id: string; state: string; title: string; updated_at: string }[];
  strikes: { kind: string; note: string; created_at: string }[];
  inputs_new: number;
}

export interface UserRow {
  id: string;
  username: string;
  display_name: string;
  role: Role;
  disabled: boolean;
  created_at: string;
  passkeys: number;
  last_used_at: string | null;
}

export interface KnowledgeDoc {
  id: string;
  channel_id: string;
  kind: string;
  version: number;
  base_version: number | null;
  status: string;
  content_sha256: string;
  proposed_by: string;
  proposed_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
}

export interface KnowledgeDraft extends KnowledgeDoc {
  content: string;
  base_content: string | null;
  current_version: number | null;
  stale: boolean;
  diff: { op: string; text: string }[];
}

export interface OwnerInput {
  id: string;
  channel_id: string;
  kind: string;
  uri: string | null;
  body: string | null;
  sha256: string;
  status: string;
  submitted_by: string;
  used_by_job: string | null;
  created_at: string | null;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

const CSRF_KEY = "hub.csrf";

export function rememberCsrf(token: string | null | undefined): void {
  if (token) sessionStorage.setItem(CSRF_KEY, token);
}

export function forgetCsrf(): void {
  sessionStorage.removeItem(CSRF_KEY);
}

async function refreshCsrf(): Promise<void> {
  const res = await fetch("/api/auth/csrf", { method: "POST", credentials: "same-origin" });
  if (res.ok) rememberCsrf(((await res.json()) as Me).csrf_token);
}

async function request<T>(method: "GET" | "POST", path: string, body?: unknown, retry = true): Promise<T> {
  if (DEMO) return (await demoRequest(method, path, body)) as T;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method === "POST") {
    if (!sessionStorage.getItem(CSRF_KEY) && !path.startsWith("/api/auth/")) await refreshCsrf();
    headers["X-CSRF-Token"] = sessionStorage.getItem(CSRF_KEY) ?? "";
  }
  const res = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 403 && retry && method === "POST" && !path.startsWith("/api/auth/")) {
    const detail = await res.clone().json().catch(() => ({}));
    if (String((detail as { detail?: string }).detail ?? "").includes("CSRF")) {
      await refreshCsrf();
      return request<T>(method, path, body, false);
    }
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    const text = typeof detail.detail === "string" ? detail.detail : JSON.stringify(detail.detail ?? "");
    throw new ApiError(res.status, `${res.status}: ${text || "request failed"}`);
  }
  return (await res.json()) as T;
}

type StepUpPurpose =
  | "approve" | "batch" | "release" | "tier_confirm" | "tier_dismiss" | "rollout_enable"
  | "rollout_signoff" | "invite" | "knowledge";
interface StepUpProof {
  ticket: string;
  credential: unknown;
}

// A fresh passkey assertion for exactly one action: the server issues a challenge bound to the
// action, the browser asks for the passkey (fingerprint, face or security key), and the proof is
// sent with the action. A stolen session cookie alone cannot approve anything.
async function stepUp(purpose: StepUpPurpose, parts: string[]): Promise<StepUpProof> {
  const res = await request<{ ticket: string; options: Record<string, unknown> }>(
    "POST",
    "/api/auth/stepup/options",
    { purpose, parts },
  );
  const credential = await startAuthentication(res.options as never);
  return { ticket: res.ticket, credential };
}

export const api = {
  me: () => request<Me>("GET", "/api/auth/me"),
  logout: () => request<{ ok: boolean }>("POST", "/api/auth/logout", {}),
  loginOptions: (username: string) => request<Record<string, unknown>>("POST", "/api/auth/login/options", { username }),
  loginVerify: (credential: unknown) => request<Me>("POST", "/api/auth/login/verify", { credential }),
  registerOptions: (invite_token: string) =>
    request<Record<string, unknown>>("POST", "/api/auth/register/options", { invite_token }),
  registerVerify: (credential: unknown) => request<Me>("POST", "/api/auth/register/verify", { credential }),

  queue: (q: Record<string, string>) => {
    const params = new URLSearchParams(Object.entries(q).filter(([, v]) => v !== ""));
    return request<QueueItem[]>("GET", `/api/jobs${params.size ? `?${params}` : ""}`);
  },
  preview: (id: string) => request<Preview>("GET", `/api/jobs/${encodeURIComponent(id)}`),
  approve: async (id: string, content_hash: string) =>
    request<{ jti: string }>("POST", `/api/jobs/${encodeURIComponent(id)}/approve`, {
      content_hash,
      stepup: await stepUp("approve", [id, content_hash]),
    }),
  edit: (id: string, body: { content_hash: string; title?: string; description?: string; tags?: string[]; script?: string }) =>
    request<{ content_hash: string; manifest_version: number }>("POST", `/api/jobs/${encodeURIComponent(id)}/edit`, body),
  reject: (id: string, body: { content_hash: string; reason: string; rework_stage: string }) =>
    request<{ ok: boolean }>("POST", `/api/jobs/${encodeURIComponent(id)}/reject`, body),
  batchApprove: async (items: { job_id: string; content_hash: string }[]) =>
    request<{ results: { job_id: string; ok: boolean; error?: string | null }[] }>("POST", "/api/jobs/batch-approve", {
      items,
      stepup: await stepUp("batch", items.map((i) => `${i.job_id}:${i.content_hash}`)),
    }),

  clusters: () => request<ClusterRow[]>("GET", "/api/clusters"),
  scorecards: (q: Record<string, string>) => {
    const params = new URLSearchParams(Object.entries(q).filter(([, v]) => v !== ""));
    return request<Scorecard[]>("GET", `/api/scorecards${params.size ? `?${params}` : ""}`);
  },
  gate: (cluster: string) => request<Gate>("GET", `/api/rollout/${encodeURIComponent(cluster)}/gate`),
  signoff: async (cluster: string, note: string) =>
    request<{ id: number }>("POST", `/api/rollout/${encodeURIComponent(cluster)}/signoff`, {
      note,
      stepup: await stepUp("rollout_signoff", [cluster]),
    }),
  enableCluster: async (cluster: string) =>
    request<{ ok: boolean }>("POST", `/api/rollout/${encodeURIComponent(cluster)}/enable`, {
      stepup: await stepUp("rollout_enable", [cluster]),
    }),

  tiers: () => request<TierRow[]>("GET", "/api/tiers"),
  tierChanges: (channel = "") =>
    request<TierChange[]>("GET", `/api/tiers/changes${channel ? `?channel=${encodeURIComponent(channel)}` : ""}`),
  tierReport: (channel = "") =>
    request<TierReport>("GET", `/api/tiers/report${channel ? `?channel=${encodeURIComponent(channel)}` : ""}`),
  confirmTier: async (id: number, note: string) =>
    request<{ ok: boolean }>("POST", `/api/tiers/recommendations/${id}/confirm`, {
      note,
      stepup: await stepUp("tier_confirm", [String(id)]),
    }),
  dismissTier: async (id: number, note: string) =>
    request<{ ok: boolean }>("POST", `/api/tiers/recommendations/${id}/dismiss`, {
      note,
      stepup: await stepUp("tier_dismiss", [String(id)]),
    }),
  recordStrike: (channel: string, body: { kind: string; note: string; video_ref?: string }) =>
    request<{ id: number }>("POST", `/api/channels/${encodeURIComponent(channel)}/strikes`, body),

  overview: () => request<Overview>("GET", "/api/overview"),
  pipeline: (q: Record<string, string> = {}) => {
    const params = new URLSearchParams(Object.entries(q).filter(([, v]) => v !== ""));
    return request<Pipeline>("GET", `/api/pipeline${params.size ? `?${params}` : ""}`);
  },
  timeline: (id: string) => request<TimelineEvent[]>("GET", `/api/jobs/${encodeURIComponent(id)}/timeline`),
  channels: () => request<ChannelRow[]>("GET", "/api/channels"),
  channel: (id: string) => request<ChannelDetail>("GET", `/api/channels/${encodeURIComponent(id)}`),
  alerts: () => request<AlertRow[]>("GET", "/api/alerts"),
  users: () => request<UserRow[]>("GET", "/api/admin/users"),
  invite: async (body: { username: string; display_name: string; role: Role }) =>
    request<{ username: string; invite_token: string; expires_at: string }>("POST", "/api/admin/invites", {
      ...body,
      stepup: await stepUp("invite", [body.username, body.role]),
    }),
  drafts: (channel = "") => request<KnowledgeDoc[]>("GET", `/api/knowledge/drafts${channel ? `?channel=${encodeURIComponent(channel)}` : ""}`),
  draft: (id: string) => request<KnowledgeDraft>("GET", `/api/knowledge/drafts/${encodeURIComponent(id)}`),
  approveDraft: async (id: string, sha: string) =>
    request<KnowledgeDoc>("POST", `/api/knowledge/drafts/${encodeURIComponent(id)}/approve`, {
      content_sha256: sha,
      stepup: await stepUp("knowledge", [id, "approve", sha]),
    }),
  rejectDraft: async (id: string, sha: string, reason: string) =>
    request<KnowledgeDoc>("POST", `/api/knowledge/drafts/${encodeURIComponent(id)}/reject`, {
      content_sha256: sha,
      reason,
      stepup: await stepUp("knowledge", [id, "reject", sha]),
    }),
  currentKnowledge: (channel: string) => request<KnowledgeDoc[]>("GET", `/api/channels/${encodeURIComponent(channel)}/knowledge`),
  inputs: (channel: string, status = "") =>
    request<OwnerInput[]>("GET", `/api/channels/${encodeURIComponent(channel)}/inputs${status ? `?status=${status}` : ""}`),
  addInput: (channel: string, body: { kind: string; body?: string; uri?: string }) =>
    request<OwnerInput>("POST", `/api/channels/${encodeURIComponent(channel)}/inputs`, body),

  controls: () => request<Controls>("GET", "/api/controls"),
  // Stopping is never blocked by the extra step; only RELEASING a switch needs the passkey.
  setGlobal: async (paused: boolean, reason: string) =>
    request<{ ok: boolean }>("POST", "/api/controls/global", {
      paused,
      reason,
      stepup: paused ? undefined : await stepUp("release", ["global", "*"]),
    }),
  setScoped: async (scope: "clusters" | "channels", id: string, paused: boolean, reason: string) =>
    request<{ ok: boolean }>("POST", `/api/controls/${scope}/${encodeURIComponent(id)}`, {
      paused,
      reason,
      stepup: paused ? undefined : await stepUp("release", [scope.slice(0, -1), id]),
    }),
};

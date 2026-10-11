// Typed client for the approval hub API (mirrors ytcore/approvals/service/openapi.json).
import { startAuthentication } from "@simplewebauthn/browser";

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

type StepUpPurpose = "approve" | "batch" | "release" | "tier_confirm" | "rollout_enable";
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
  signoff: (cluster: string, note: string) =>
    request<{ id: number }>("POST", `/api/rollout/${encodeURIComponent(cluster)}/signoff`, { note }),
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
  dismissTier: (id: number, note: string) =>
    request<{ ok: boolean }>("POST", `/api/tiers/recommendations/${id}/dismiss`, { note }),
  recordStrike: (channel: string, body: { kind: string; note: string; video_ref?: string }) =>
    request<{ id: number }>("POST", `/api/channels/${encodeURIComponent(channel)}/strikes`, body),

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

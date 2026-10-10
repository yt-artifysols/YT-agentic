// Typed client for the approval hub API (mirrors ytcore/approvals/service/openapi.json).

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
  previous: { version: number; metadata: Record<string, unknown> } | null;
}

export interface Controls {
  global_paused: boolean;
  clusters: { id: string; name: string; status: string }[];
  channels: { id: string; name: string; cluster_id: string | null; status: string; tier: string }[];
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
  approve: (id: string, content_hash: string) =>
    request<{ jti: string }>("POST", `/api/jobs/${encodeURIComponent(id)}/approve`, { content_hash }),
  edit: (id: string, body: { content_hash: string; title?: string; description?: string; tags?: string[]; script?: string }) =>
    request<{ content_hash: string; manifest_version: number }>("POST", `/api/jobs/${encodeURIComponent(id)}/edit`, body),
  reject: (id: string, body: { content_hash: string; reason: string; rework_stage: string }) =>
    request<{ ok: boolean }>("POST", `/api/jobs/${encodeURIComponent(id)}/reject`, body),
  batchApprove: (items: { job_id: string; content_hash: string }[]) =>
    request<{ results: { job_id: string; ok: boolean; error?: string | null }[] }>("POST", "/api/jobs/batch-approve", { items }),

  controls: () => request<Controls>("GET", "/api/controls"),
  setGlobal: (paused: boolean, reason: string) => request<{ ok: boolean }>("POST", "/api/controls/global", { paused, reason }),
  setScoped: (scope: "clusters" | "channels", id: string, paused: boolean, reason: string) =>
    request<{ ok: boolean }>("POST", `/api/controls/${scope}/${encodeURIComponent(id)}`, { paused, reason }),
};

// Presentation helpers: times, countdowns and the vocabulary shown to people (never raw enums).

export function relTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "-";
  const diff = new Date(iso).getTime() - now;
  const abs = Math.abs(diff);
  const m = Math.round(abs / 60_000);
  const text =
    m < 1 ? "now" : m < 60 ? `${m} min` : m < 60 * 24 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} d`;
  if (m < 1) return "just now";
  return diff < 0 ? `${text} ago` : `in ${text}`;
}

export function countdown(iso: string | null | undefined, now = Date.now()): { text: string; tone: "ok" | "warn" | "bad" | "" } {
  if (!iso) return { text: "no deadline", tone: "" };
  const ms = new Date(iso).getTime() - now;
  if (ms <= 0) return { text: "deadline passed", tone: "bad" };
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  const text = h > 0 ? `${h} h ${m} min left` : `${m} min left`;
  return { text, tone: ms < 2 * 3_600_000 ? "warn" : "ok" };
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function money(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "-";
  return `$${(cents / 100).toFixed(cents % 100 === 0 ? 0 : 2)}`;
}

export function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "-" : `${Math.round(v * 100)}%`;
}

export const STATE_LABEL: Record<string, string> = {
  planned: "Planned",
  researching: "Researching",
  scripting: "Writing script",
  rendering: "Rendering",
  qa: "Quality check",
  awaiting_approval: "Needs your approval",
  approved: "Approved",
  scheduled: "Scheduled",
  publishing: "Publishing",
  published: "Published",
  live: "Live",
  rejected: "Rejected",
  failed: "Failed",
  cancelled: "Cancelled",
};

export type Tone = "ok" | "warn" | "bad" | "info" | "brand" | "";

export const STATE_TONE: Record<string, Tone> = {
  planned: "",
  researching: "info",
  scripting: "info",
  rendering: "info",
  qa: "info",
  awaiting_approval: "warn",
  approved: "brand",
  scheduled: "brand",
  publishing: "brand",
  published: "ok",
  live: "ok",
  rejected: "bad",
  failed: "bad",
  cancelled: "",
};

export const PIPELINE_ORDER = [
  "planned", "researching", "scripting", "rendering", "qa", "awaiting_approval",
  "approved", "scheduled", "publishing", "published", "live",
];

export function riskTone(risk: number | null): Tone {
  if (risk === null) return "bad";
  return risk >= 60 ? "bad" : risk >= 25 ? "warn" : "ok";
}

export function riskLabel(risk: number | null): string {
  if (risk === null) return "No quality report";
  return risk >= 60 ? "High risk" : risk >= 25 ? "Medium risk" : "Low risk";
}

export const LANE_LABEL: Record<string, string> = {
  standard: "Review",
  fast: "Fast lane",
  deep: "Deep review",
};

export function initials(name: string): string {
  return name
    .split(/\s+/)
    .map((p) => p[0] ?? "")
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

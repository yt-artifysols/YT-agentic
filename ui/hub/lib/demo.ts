// Demo mode: the same screens with in-browser sample data (no server, nothing real).
// Enabled only by a build flag (NEXT_PUBLIC_DEMO=1) used for design review; the production
// build never contains it. Decisions here only change the sample state in this tab.

export const DEMO = process.env.NEXT_PUBLIC_DEMO === "1";

type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const H = 3_600_000;
const iso = (offsetMs: number) => new Date(Date.now() + offsetMs).toISOString();
const hash = (s: string) => "sha256:" + Array.from(s).reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7).toString(16).padStart(8, "0").repeat(8);

const CHANNELS = [
  { id: "finance-daily", name: "Finance Daily", cluster_id: "cluster-a", tier: 2, tz: "America/New_York", budget: 900 },
  { id: "money-basics", name: "Money Basics", cluster_id: "cluster-a", tier: 1, tz: "Europe/London", budget: 800 },
  { id: "diy-weekly", name: "DIY Weekly", cluster_id: "cluster-b", tier: 1, tz: "America/Chicago", budget: 800 },
  { id: "garage-hacks", name: "Garage Hacks", cluster_id: "cluster-b", tier: 1, tz: "Australia/Sydney", budget: 700 },
  { id: "space-minute", name: "Space Minute", cluster_id: "cluster-c", tier: 1, tz: "UTC", budget: 600 },
];

const THEMES = ["#4a56e2", "#0f9d8a", "#c2410c", "#7b5cf0", "#0e7490", "#b45309"];
function art(title: string, i: number): string {
  const c = THEMES[i % THEMES.length];
  const words = title.split(" ").slice(0, 5).join(" ");
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='1280' height='720'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='${c}'/><stop offset='1' stop-color='#111634'/></linearGradient></defs><rect width='1280' height='720' fill='url(#g)'/><circle cx='1040' cy='170' r='210' fill='rgba(255,255,255,.08)'/><text x='70' y='400' font-family='Arial' font-weight='800' font-size='84' fill='white'>${words.replace(/&/g, "and").replace(/</g, "")}</text><text x='72' y='470' font-family='Arial' font-size='34' fill='rgba(255,255,255,.75)'>Demo thumbnail</text></svg>`;
  return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
}

interface Job {
  id: string;
  channel: string;
  state: string;
  title: string;
  risk: number | null;
  flags: Json[];
  deadline: number | null;
  updated: number;
  lane: string;
  reason: string;
  script: string;
  description: string;
  tags: string[];
  version: number;
}

const SCRIPT_A = `Hook: Most people think the 50/30/20 rule is a rule. It is a starting point.\n\nPoint one: Needs come first. Rent, groceries, minimum debt payments.\nPoint two: Wants are not the enemy. Cut by percent, not by shame.\nPoint three: Pay yourself before anything else gets a vote.\n\nClose: Pick one number this week and change it. Then check back in 30 days.`;
const SCRIPT_B = `Hook: This $12 fix makes a squeaky door silent in 40 seconds.\n\nStep one: Remove the hinge pin with a nail and a hammer.\nStep two: Rub a bar of soap along the pin. Yes, plain soap.\nStep three: Tap it back in, open and close the door twice.\n\nClose: If it still squeaks, the hinge plate is loose. Tighten the screws.`;

function mkJobs(): Job[] {
  const base = (n: number, channel: string, state: string, title: string, risk: number | null, o: Partial<Job> = {}): Job => ({
    id: `7d1c0e5a-0b1f-4c1e-9a${n.toString().padStart(2, "0")}-3f5e8a6b7c${n.toString().padStart(2, "0")}`,
    channel,
    state,
    title,
    risk,
    flags: [],
    deadline: null,
    updated: Date.now() - n * 17 * 60_000,
    lane: "standard",
    reason: "Approve-all tier: every video is reviewed one by one.",
    script: channel.includes("diy") || channel.includes("garage") ? SCRIPT_B : SCRIPT_A,
    description: `${title}\n\nA short, practical explainer. Sources and notes are in the pinned comment.`,
    tags: ["explainer", "how to", "2026"],
    version: 1,
    ...o,
  });
  return [
    base(1, "finance-daily", "awaiting_approval", "Why the 50/30/20 rule breaks at high rent", 8, { deadline: Date.now() + 3.2 * H, lane: "fast", reason: "Fast lane: low risk, no flags." }),
    base(2, "finance-daily", "awaiting_approval", "Index funds in plain words: what you really own", 12, { deadline: Date.now() + 4.1 * H, lane: "fast", reason: "Fast lane: low risk, no flags." }),
    base(3, "finance-daily", "awaiting_approval", "3 bank fees you can get refunded this week", 31, { deadline: Date.now() + 1.4 * H, lane: "deep", reason: "Deep review: spot check of a low-risk video.", flags: [{ code: "unsupported_claim", severity: "warn", detail: "“Everyone can get a refund” is not supported by the cited sources.", evidence: "…you will get every fee refunded…" }] }),
    base(4, "money-basics", "awaiting_approval", "Credit scores explained in 4 minutes", 44, { deadline: Date.now() + 5.6 * H, flags: [{ code: "repetitive_content", severity: "warn", detail: "Similar to an earlier video on this channel (0.35 similarity).", evidence: "" }, { code: "title_template", severity: "warn", detail: "The title format is used in 4 other jobs of this fleet.", evidence: "" }] }),
    base(5, "diy-weekly", "awaiting_approval", "Silence a squeaky door in 40 seconds", 18, { deadline: Date.now() + 0.7 * H }),
    base(6, "finance-daily", "rendering", "Should you pay off the car loan early?", null, { updated: Date.now() - 8 * 60_000 }),
    base(7, "money-basics", "qa", "What a 401(k) match really is", null, { updated: Date.now() - 3 * 60_000 }),
    base(8, "diy-weekly", "scripting", "Fix a leaking tap without a plumber", null),
    base(9, "garage-hacks", "researching", "Garage shelving from one sheet of plywood", null),
    base(10, "space-minute", "planned", "Why the Moon is slowly leaving us", null),
    base(11, "finance-daily", "live", "How compound interest actually works", 6, { updated: Date.now() - 5 * H }),
    base(12, "money-basics", "live", "Needs vs wants: a simple test", 9, { updated: Date.now() - 22 * H }),
    base(13, "diy-weekly", "rejected", "Rewire a lamp in 10 minutes", 71, { updated: Date.now() - 30 * H, flags: [{ code: "safety_claim", severity: "block", detail: "Electrical work shown without a safety warning.", evidence: "" }] }),
    base(14, "garage-hacks", "failed", "Rust-proof your tools", null, { updated: Date.now() - 26 * H }),
  ];
}

interface DemoState {
  jobs: Job[];
  paused: { global: boolean; clusters: Record<string, boolean>; channels: Record<string, boolean> };
  tierRecs: { id: number; channel: string; evidence: Json }[];
  tiers: Record<string, number>;
  drafts: Json[];
  inputs: Json[];
  users: Json[];
  alerts: Json[];
}

let state: DemoState | null = null;
function get(): DemoState {
  if (state) return state;
  state = {
    jobs: mkJobs(),
    paused: { global: false, clusters: {}, channels: {} },
    tierRecs: [{ id: 1, channel: "money-basics", evidence: { days_observed: 34, decisions: 27, rejected: 1, edited: 3, avg_risk: 17.4, cost_per_video_cents: 41, strikes: 0, policy_flags: 0 } }],
    tiers: Object.fromEntries(CHANNELS.map((c) => [c.id, c.tier])),
    drafts: [
      { id: "d-1", channel_id: "finance-daily", kind: "banned_claims", version: 3, base_version: 2, status: "draft", content_sha256: hash("d1"), proposed_by: "agent:script", proposed_at: iso(-2 * H), approved_by: null, approved_at: null, content: "guaranteed returns\nget rich quick\nrisk-free\nno risk\ndouble your money", base_content: "guaranteed returns\nget rich quick\nrisk-free", current_version: 2, stale: false, diff: [{ op: "same", text: "guaranteed returns" }, { op: "same", text: "get rich quick" }, { op: "same", text: "risk-free" }, { op: "add", text: "no risk" }, { op: "add", text: "double your money" }] },
      { id: "d-2", channel_id: "diy-weekly", kind: "brand_voice", version: 2, base_version: 1, status: "draft", content_sha256: hash("d2"), proposed_by: "config:diy-weekly", proposed_at: iso(-26 * H), approved_by: null, approved_at: null, content: "Friendly, practical, a little dry humour. Always show the safety step first.", base_content: "Friendly and practical.", current_version: 1, stale: false, diff: [{ op: "del", text: "Friendly and practical." }, { op: "add", text: "Friendly, practical, a little dry humour. Always show the safety step first." }] },
    ],
    inputs: [
      { id: "i-1", channel_id: "finance-daily", kind: "idea", uri: null, body: "Explain why savings accounts pay so little compared with money-market funds.", sha256: hash("i1"), status: "new", submitted_by: "owner", used_by_job: null, created_at: iso(-30 * H) },
      { id: "i-2", channel_id: "finance-daily", kind: "goal", uri: null, body: "Grow watch time on beginner investing videos this month.", sha256: hash("i2"), status: "new", submitted_by: "owner", used_by_job: null, created_at: iso(-72 * H) },
      { id: "i-3", channel_id: "diy-weekly", kind: "footage", uri: "gs://owner-footage/door-hinge.mp4", body: "Hinge close-up", sha256: hash("i3"), status: "used", submitted_by: "owner", used_by_job: "x", created_at: iso(-90 * H) },
    ],
    users: [
      { id: "u1", username: "owner", display_name: "Alex Owner", role: "admin", disabled: false, created_at: iso(-400 * H), passkeys: 2, last_used_at: iso(-1 * H) },
      { id: "u2", username: "maria", display_name: "Maria Reviewer", role: "approver", disabled: false, created_at: iso(-300 * H), passkeys: 1, last_used_at: iso(-6 * H) },
      { id: "u3", username: "sam", display_name: "Sam Operator", role: "operator", disabled: false, created_at: iso(-200 * H), passkeys: 1, last_used_at: iso(-30 * H) },
    ],
    alerts: [
      { id: 5, kind: "tier.recommended", message: "channel money-basics earned a promotion recommendation; an admin must confirm it", channel_id: "money-basics", cluster_id: null, created_at: iso(-3 * H) },
      { id: 4, kind: "budget.warning", message: "channel finance-daily has used 82% of its daily budget", channel_id: "finance-daily", cluster_id: null, created_at: iso(-5 * H) },
      { id: 3, kind: "job.stuck", message: "job idle in rendering for 70 minutes", channel_id: "garage-hacks", cluster_id: null, created_at: iso(-27 * H) },
      { id: 2, kind: "tier.demoted", message: "channel garage-hacks returned to approve-all: rejection spike", channel_id: "garage-hacks", cluster_id: null, created_at: iso(-60 * H) },
    ],
  };
  return state;
}

export function demoRole(): string {
  const r = typeof location !== "undefined" ? new URLSearchParams(location.search).get("role") : null;
  return r === "admin" || r === "operator" ? r : "approver";
}

const chan = (id: string) => CHANNELS.find((c) => c.id === id)!;
const clusterOf = (id: string) => chan(id)?.cluster_id ?? null;

function queueItem(j: Job, s: DemoState): Json {
  const tier = s.tiers[j.channel] ?? 1;
  const lane = tier === 2 ? (j.risk !== null && j.risk <= 20 && j.flags.length === 0 ? j.lane : "deep") : "standard";
  return {
    job_id: j.id, channel_id: j.channel, cluster_id: clusterOf(j.channel), title: j.title, origin: "planner",
    content_hash: hash(j.id + j.version), manifest_version: j.version, risk_score: j.risk, qa_passed: j.flags.every((f) => f.severity !== "block"),
    flag_count: j.flags.length, approval_deadline: j.deadline ? new Date(j.deadline).toISOString() : null,
    fast_lane: lane === "fast", lane, lane_reason: lane === "standard" ? "Approve-all tier: every video is reviewed one by one." : j.reason,
  };
}

function overview(s: DemoState): Json {
  const waiting = s.jobs.filter((j) => j.state === "awaiting_approval");
  const deadlines = waiting.map((j) => j.deadline).filter((d): d is number => d !== null).sort();
  return {
    generated_at: iso(0),
    awaiting_approval: { count: waiting.length, oldest_deadline: deadlines[0] ? new Date(deadlines[0]).toISOString() : null, due_within_2h: deadlines.filter((d) => d < Date.now() + 2 * H).length },
    in_production: s.jobs.filter((j) => ["planned", "researching", "scripting", "rendering", "qa"].includes(j.state)).length,
    last_24h: { live: s.jobs.filter((j) => j.state === "live").length, failed: 1, cancelled: 0, rejected: 1 },
    kill_switch: { global_paused: s.paused.global, paused_clusters: Object.values(s.paused.clusters).filter(Boolean).length, paused_channels: Object.values(s.paused.channels).filter(Boolean).length },
    cost_today_cents: 312, daily_budget_cents: 3800, alerts_24h: s.alerts.filter((a) => Date.now() - new Date(a.created_at).getTime() < 24 * H).length,
    alerts: s.alerts.slice(0, 5),
    activity: s.jobs.slice(0, 9).map((j, i) => ({ id: 100 - i, job_id: j.id, channel_id: j.channel, from_state: null, to_state: j.state, actor: j.state === "live" ? "gateway" : "workflow", at: new Date(j.updated).toISOString(), title: j.title })),
    clusters: [
      { id: "cluster-a", name: "Personal finance", status: "active", live_enabled: true, channels: 2, awaiting_approval: waiting.filter((j) => clusterOf(j.channel) === "cluster-a").length },
      { id: "cluster-b", name: "Home & DIY", status: "active", live_enabled: false, channels: 2, awaiting_approval: waiting.filter((j) => clusterOf(j.channel) === "cluster-b").length },
      { id: "cluster-c", name: "Science", status: "active", live_enabled: false, channels: 1, awaiting_approval: 0 },
    ],
    tiers: { pending_recommendations: s.tierRecs.length, demotions_7d: 1 },
    channels: CHANNELS.length,
  };
}

function route(method: string, path: string, body: Json | undefined): unknown {
  const s = get();
  const [p, qs] = path.split("?");
  const q = new URLSearchParams(qs ?? "");
  const m = (re: RegExp) => p!.match(re);
  const ok = { ok: true };

  if (p === "/api/auth/me") return { username: "owner", display_name: "Alex Owner", role: demoRole(), csrf_token: "demo" };
  if (p === "/api/auth/logout") return ok;
  if (p === "/api/overview") return overview(s);
  if (p === "/api/jobs" && method === "GET") {
    let items = s.jobs.filter((j) => j.state === "awaiting_approval").map((j) => queueItem(j, s));
    if (q.get("channel")) items = items.filter((i) => i.channel_id === q.get("channel"));
    if (q.get("cluster")) items = items.filter((i) => i.cluster_id === q.get("cluster"));
    if (q.get("fast_lane") === "true") items = items.filter((i) => i.fast_lane);
    const sort = q.get("sort") ?? "risk";
    items.sort((a, b) => (sort === "deadline" ? (a.approval_deadline ?? "").localeCompare(b.approval_deadline ?? "") : (b.risk_score ?? 100) - (a.risk_score ?? 100)));
    return items;
  }
  if (p === "/api/pipeline") {
    const counts: Json = {};
    s.jobs.forEach((j) => (counts[j.state] = (counts[j.state] ?? 0) + 1));
    let items = s.jobs;
    if (q.get("state")) items = items.filter((j) => j.state === q.get("state"));
    if (q.get("channel")) items = items.filter((j) => j.channel === q.get("channel"));
    return {
      counts,
      items: items.map((j) => ({ job_id: j.id, channel_id: j.channel, cluster_id: clusterOf(j.channel), state: j.state, origin: "planner", title: j.title, created_at: new Date(j.updated - 3 * H).toISOString(), updated_at: new Date(j.updated).toISOString(), approval_deadline: j.deadline ? new Date(j.deadline).toISOString() : null, risk_score: j.risk, youtube_video_id: j.state === "live" ? "dQw4w9WgXcQ".slice(0, 11) : null })),
    };
  }
  let r = m(/^\/api\/jobs\/([^/]+)\/timeline$/);
  if (r) {
    const j = s.jobs.find((x) => x.id === r![1]);
    if (!j) throw new Error("404: job not found");
    const chain = ["planned", "researching", "scripting", "rendering", "qa", "awaiting_approval"];
    return chain.slice(0, Math.max(1, chain.indexOf(j.state) + 1 || chain.length)).map((st, i, a) => ({ id: i, from_state: i ? a[i - 1] : null, to_state: st, actor: i === 0 ? "planner" : "workflow", at: new Date(j.updated - (a.length - i) * 11 * 60_000).toISOString(), payload: {} }));
  }
  r = m(/^\/api\/jobs\/([^/]+)$/);
  if (r && method === "GET") {
    const j = s.jobs.find((x) => x.id === r![1]);
    if (!j) throw new Error("404: job not found");
    const qi = queueItem(j, s);
    return {
      job_id: j.id, channel_id: j.channel, state: j.state, content_hash: qi.content_hash, manifest_version: j.version, title: j.title, description: j.description, tags: j.tags, script: j.script,
      metadata: { category: "Education", privacy_status: "private", made_for_kids: false }, assets: [{ name: "thumbnail", digest: hash("t"), url: art(j.title, parseInt(j.id.slice(-2), 10)) }],
      video_url: null, thumbnail_url: art(j.title, parseInt(j.id.slice(-2), 10)),
      qa: j.risk === null ? null : { passed: true, risk_score: j.risk, flags: j.flags, sources: [{ url: "https://www.consumerfinance.gov/", title: "Consumer Financial Protection Bureau" }, { url: "https://www.investor.gov/", title: "Investor.gov" }], for_current_content: true },
      fast_lane: qi.fast_lane, lane: qi.lane, lane_reason: qi.lane_reason, previous: j.version > 1 ? { version: j.version - 1, metadata: { title: "Earlier title" } } : null,
    };
  }
  r = m(/^\/api\/jobs\/([^/]+)\/(approve|reject|edit)$/);
  if (r) {
    const j = s.jobs.find((x) => x.id === r![1]);
    if (!j) throw new Error("404: job not found");
    if (r[2] === "approve") { j.state = "scheduled"; j.updated = Date.now(); return { job_id: j.id, content_hash: hash(j.id), jti: "demo", approver: "owner" }; }
    if (r[2] === "reject") { j.state = "rejected"; j.updated = Date.now(); return ok; }
    j.version += 1; if (body?.title) j.title = body.title; if (body?.script) j.script = body.script; j.state = "qa"; setTimeout(() => (j.state = "awaiting_approval"), 1200);
    return { job_id: j.id, content_hash: hash(j.id + j.version), manifest_version: j.version };
  }
  if (p === "/api/jobs/batch-approve") {
    return { results: (body?.items ?? []).map((i: Json) => { const j = s.jobs.find((x) => x.id === i.job_id); if (j) { j.state = "scheduled"; j.updated = Date.now(); } return { job_id: i.job_id, ok: !!j, jti: "demo" }; }) };
  }
  r = m(/^\/api\/channels\/([^/]+)$/);
  if (r && method === "GET") {
    const row = (route("GET", "/api/channels", undefined) as Json[]).find((c) => c.id === r![1]);
    if (!row) throw new Error("404: channel not found");
    return { ...row, knowledge: ["brand_voice", "policy", "banned_claims", "hook_library"].map((k) => ({ kind: k, version: 2, status: "approved", proposed_at: iso(-200 * H) })), recent_jobs: s.jobs.filter((j) => j.channel === r![1]).slice(0, 6).map((j) => ({ job_id: j.id, state: j.state, title: j.title, updated_at: new Date(j.updated).toISOString() })), strikes: r[1] === "garage-hacks" ? [{ kind: "warning", note: "Community guidelines warning on the 'Rust-proof' video.", created_at: iso(-60 * H) }] : [], inputs_new: s.inputs.filter((i) => i.channel_id === r![1] && i.status === "new").length };
  }
  if (p === "/api/channels") {
    return CHANNELS.map((c) => ({
      id: c.id, name: c.name, cluster_id: c.cluster_id, status: s.paused.channels[c.id] ? "paused" : "active", autonomy: c.cluster_id === "cluster-a" ? "gated" : "shadow", approval_tier: s.tiers[c.id], daily_budget_cents: c.budget, timezone: c.tz,
      awaiting_approval: s.jobs.filter((j) => j.channel === c.id && j.state === "awaiting_approval").length, in_production: s.jobs.filter((j) => j.channel === c.id && ["planned", "researching", "scripting", "rendering", "qa"].includes(j.state)).length,
      published_7d: s.jobs.filter((j) => j.channel === c.id && j.state === "live").length + 3, next_slot: iso(9 * H), approval_rate_30d: c.cluster_id === "cluster-a" ? 0.93 : 0.8,
    }));
  }
  if (p === "/api/alerts") return s.alerts;
  if (p === "/api/admin/users") return s.users;
  if (p === "/api/admin/invites") return { username: body?.username, invite_token: "demo-invite-token-0000", expires_at: iso(24 * H) };
  if (p === "/api/clusters") {
    return [
      { id: "cluster-a", display_name: "Personal finance", niche: "Everyday money", status: "active", live_enabled: true, live_since: iso(-16 * 24 * H), requires_cluster: null, gate_days: 14, max_concurrent_jobs: 3, channels: 2, awaiting_approval: 4, oldest_waiting_since: iso(-4 * H), health: { running: 1, pending: 0, budget_held: false } },
      { id: "cluster-b", display_name: "Home & DIY", niche: "Practical home fixes", status: "active", live_enabled: false, live_since: null, requires_cluster: "cluster-a", gate_days: 14, max_concurrent_jobs: 3, channels: 2, awaiting_approval: 1, oldest_waiting_since: iso(-1 * H), health: null },
      { id: "cluster-c", display_name: "Science", niche: "Space and physics", status: "active", live_enabled: false, live_since: null, requires_cluster: "cluster-b", gate_days: 14, max_concurrent_jobs: 3, channels: 1, awaiting_approval: 0, oldest_waiting_since: null, health: null },
    ];
  }
  r = m(/^\/api\/rollout\/([^/]+)\/gate$/);
  if (r) return { cluster_id: r[1], live: r[1] === "cluster-a", live_days: 16, required_days: 14, breaker_trips: 0, stuck_or_plan_alerts: 1, failed_uploads: 0, pending_uploads: 0, decisions: 41, rejection_rate: 0.07, scorecard_days: 14, problems: r[1] === "cluster-a" ? [] : ["cluster-a has not signed off yet"], ok: r[1] === "cluster-a" };
  if (p!.startsWith("/api/rollout/")) return { id: 1, ok: true };
  if (p === "/api/scorecards") {
    const rows: Json[] = [];
    for (const c of CHANNELS.slice(0, 4)) for (let d = 0; d < 14; d++) rows.push({ channel_id: c.id, cluster_id: c.cluster_id, score_date: new Date(Date.now() - d * 24 * H).toISOString().slice(0, 10), jobs_planned: 1, jobs_approved: d % 6 === 5 ? 0 : 1, jobs_rejected: d % 6 === 5 ? 1 : 0, approval_rate: d % 6 === 5 ? 0 : 1, policy_flags: d % 9 === 0 ? 1 : 0, videos_published: d % 6 === 5 ? 0 : 1, cost_cents: 30 + ((d * 7) % 20), cost_per_video: 38 + ((d * 5) % 12), avg_ctr: 0.04 + (d % 5) / 100, avg_retention: 0.42 + (d % 4) / 50, views: 800 + d * 120, revenue_cents: null });
    return rows;
  }
  if (p === "/api/controls") return { global_paused: s.paused.global, clusters: [["cluster-a", "Personal finance"], ["cluster-b", "Home & DIY"], ["cluster-c", "Science"]].map(([id, name]) => ({ id, name, status: s.paused.clusters[id!] ? "paused" : "active" })), channels: CHANNELS.map((c) => ({ id: c.id, name: c.name, cluster_id: c.cluster_id, status: s.paused.channels[c.id] ? "paused" : "active", tier: c.cluster_id === "cluster-a" ? "gated" : "shadow" })) };
  if (p === "/api/controls/global") { s.paused.global = !!body?.paused; return ok; }
  r = m(/^\/api\/controls\/(clusters|channels)\/([^/]+)$/);
  if (r) { s.paused[r[1] as "clusters" | "channels"][r[2]!] = !!body?.paused; return ok; }
  if (p === "/api/tiers") return CHANNELS.map((c) => { const rec = s.tierRecs.find((x) => x.channel === c.id); return { channel_id: c.id, name: c.name, cluster_id: c.cluster_id, tier: s.tiers[c.id], tier_since: iso(-40 * 24 * H), last_change_kind: c.id === "garage-hacks" ? "demotion" : c.id === "finance-daily" ? "promotion" : null, last_change_reason: c.id === "garage-hacks" ? "rejection spike: 3 of the last 10 decisions" : c.id === "finance-daily" ? "reviewed the first month myself" : null, last_change_at: iso(-20 * 24 * H), recommendation_id: rec?.id ?? null, recommendation_evidence: rec?.evidence ?? null }; });
  if (p === "/api/tiers/changes") return [
    { id: 3, channel_id: "garage-hacks", from_tier: 2, to_tier: 1, kind: "demotion", reason: "rejection spike: 3 of the last 10 decisions", actor: "system:tiers", evidence: {}, recommendation_id: null, created_at: iso(-60 * H) },
    { id: 2, channel_id: "finance-daily", from_tier: 1, to_tier: 2, kind: "promotion", reason: "reviewed the first month myself", actor: "hub:owner", evidence: {}, recommendation_id: 1, created_at: iso(-20 * 24 * H) },
  ];
  if (p === "/api/tiers/report") return { spot_checks: { n: 11, disagreements: 1, rate: 0.0909 }, low_risk_all: { n: 63, disagreements: 4, rate: 0.0635 }, by_risk_bucket: { "0-19": { n: 58, not_approved: 3 }, "20-39": { n: 21, not_approved: 3 }, "40-59": { n: 9, not_approved: 4 }, "60-79": { n: 5, not_approved: 4 } } };
  r = m(/^\/api\/tiers\/recommendations\/(\d+)\/(confirm|dismiss)$/);
  if (r) { const rec = s.tierRecs.find((x) => x.id === Number(r![1])); if (rec && r[2] === "confirm") s.tiers[rec.channel] = 2; s.tierRecs = s.tierRecs.filter((x) => x.id !== Number(r![1])); return ok; }
  if (p === "/api/knowledge/drafts") return s.drafts.filter((d) => d.status === "draft" && (!q.get("channel") || d.channel_id === q.get("channel"))).map(({ content, base_content, diff, current_version, stale, ...rest }) => rest); // eslint-disable-line @typescript-eslint/no-unused-vars
  r = m(/^\/api\/knowledge\/drafts\/([^/]+)$/);
  if (r) { const d = s.drafts.find((x) => x.id === r![1]); if (!d) throw new Error("404: draft not found"); return d; }
  r = m(/^\/api\/knowledge\/drafts\/([^/]+)\/(approve|reject)$/);
  if (r) { const d = s.drafts.find((x) => x.id === r![1]); if (d) d.status = r[2] === "approve" ? "approved" : "rejected"; return d; }
  r = m(/^\/api\/channels\/([^/]+)\/knowledge$/);
  if (r) return ["brand_voice", "policy", "banned_claims"].map((k, i) => ({ id: `k-${i}`, channel_id: r![1], kind: k, version: 2, base_version: 1, status: "approved", content_sha256: hash(k), proposed_by: "config", proposed_at: iso(-300 * H), approved_by: "owner", approved_at: iso(-290 * H) }));
  r = m(/^\/api\/channels\/([^/]+)\/inputs$/);
  if (r && method === "GET") return s.inputs.filter((i) => i.channel_id === r![1]);
  if (r && method === "POST") { const item = { id: `i-${s.inputs.length + 1}`, channel_id: r[1], kind: body?.kind, uri: body?.uri ?? null, body: body?.body ?? null, sha256: hash(String(body?.body ?? body?.uri)), status: "new", submitted_by: "owner", used_by_job: null, created_at: iso(0) }; s.inputs.unshift(item); return item; }
  r = m(/^\/api\/channels\/([^/]+)\/strikes$/);
  if (r) return { id: 1 };
  throw new Error(`404: demo has no ${method} ${p}`);
}

export async function demoRequest(method: string, path: string, body?: unknown): Promise<unknown> {
  await new Promise((r) => setTimeout(r, 120));
  return route(method, path, body as Json | undefined);
}

// Static export needs the dynamic route params up front.
export const DEMO_JOB_IDS: string[] = mkJobs().map((j) => j.id);
export const CHANNEL_IDS: string[] = CHANNELS.map((c) => c.id);

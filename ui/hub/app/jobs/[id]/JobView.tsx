"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Icon } from "@/components/icons";
import { Badge, Button, Card, Field, Notice, RiskBadge, SkeletonList, StateBadge, Tabs, useToast } from "@/components/ui";
import { ApiError, api, type Preview, type TimelineEvent } from "@/lib/api";
import { asText, diffLines } from "@/lib/diff";
import { LANE_LABEL, STATE_LABEL, dateTime } from "@/lib/format";
import { canDecide, useSession } from "@/lib/session";

type Mode = "view" | "edit" | "reject";
type Tab = "script" | "checks" | "history";

function safeHref(value: unknown): string | null {
  // Sources are untrusted data: only plain http(s) links are made clickable.
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

function Diff({ label, before, after }: { label: string; before: string; after: string }) {
  if (before === after) return null;
  return (
    <div>
      <div className="muted small">{label}</div>
      <pre className="diff" data-testid={`diff-${label}`}>
        {diffLines(before, after).map((l, i) => (
          <span key={i} className={l.kind === "same" ? undefined : l.kind}>
            {l.kind === "add" ? "+ " : l.kind === "del" ? "- " : "  "}
            {l.text}
            {"\n"}
          </span>
        ))}
      </pre>
    </div>
  );
}

export function JobView() {
  const { me, refresh } = useSession();
  const router = useRouter();
  const toast = useToast();
  const { id } = useParams<{ id: string }>();
  const [job, setJob] = useState<Preview | null>(null);
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [mode, setMode] = useState<Mode>("view");
  const [tab, setTab] = useState<Tab>("script");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [wide, setWide] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [edit, setEdit] = useState({ title: "", description: "", tags: "", script: "" });
  const [reject, setReject] = useState({ reason: "", rework_stage: "script" });

  const load = useCallback(async () => {
    const p = await api.preview(id);
    setJob(p);
    setEdit({ title: p.title, description: p.description, tags: p.tags.join(", "), script: p.script });
    api.timeline(id).then(setEvents).catch(() => undefined);
  }, [id]);

  useEffect(() => {
    if (me) load().catch((e: Error) => setError(e.message));
  }, [me, load]);
  useEffect(() => setReviewed(false), [job?.content_hash]);
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1100px)");
    const on = () => setWide(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  const act = useCallback(
    async (fn: () => Promise<unknown>, done: string) => {
      setBusy(true);
      setError(null);
      try {
        await fn();
        void refresh();
        router.push(`/review?done=${encodeURIComponent(done)}`);
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          setError("This video changed since you opened it. The latest version is shown; review it again.");
          setMode("view");
          await load();
        } else setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [router, load, refresh],
  );

  const decide = canDecide(me) && job?.state === "awaiting_approval";

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLSelectElement;
      if (e.key === "Escape") {
        if (mode !== "view") setMode("view");
        else router.push("/review");
        return;
      }
      if (typing || !decide || busy) return;
      // No one-key approve: approval is a button behind the "I reviewed this" confirmation.
      if (e.key === "e") setMode("edit");
      else if (e.key === "r") setMode("reject");
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, decide, busy, router]);

  if (!job) return error ? <Notice tone="err">{error}</Notice> : <SkeletonList rows={6} />;
  const prev = job.previous?.metadata;
  const blocking = job.qa ? job.qa.flags.some((f) => f.severity === "block") || !job.qa.passed : false;

  const decision = decide ? (
      <Card title="Your decision">
        <div className="stack">
          <label className="check">
            <input type="checkbox" data-testid="reviewed" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} />
            <span>I reviewed the video, thumbnail, script and flags of this version</span>
          </label>
          <Button variant="success" icon="check" className="block" disabled={!reviewed} busy={busy} data-testid="approve" onClick={() => void act(() => api.approve(job.job_id, job.content_hash), "approved")}>
            Approve with passkey
          </Button>
          <div className="row">
            <Button icon="edit" disabled={busy} onClick={() => setMode("edit")} data-testid="edit">Edit</Button>
            <Button variant="danger" icon="x" disabled={busy} onClick={() => setMode("reject")} data-testid="reject">Reject</Button>
          </div>
          <div className="faint small">Approving signs a token for exactly this version. Edit shortcut: e · Reject: r.</div>
        </div>
      </Card>
  ) : (
    <Notice tone="info">{canDecide(me) ? `This video is ${STATE_LABEL[job.state] ?? job.state}; no decision is needed.` : "Only approvers can decide on videos."}</Notice>
  );

  return (
    <div className="stack">
      <Link href="/review" className="row small muted"><span style={{ display: "inline-flex", transform: "rotate(180deg)" }}><Icon name="chevron" size={14} /></span> Back to review</Link>
      <div className="row between">
        <div className="stack-sm" style={{ minWidth: 0 }}>
          <h1 data-testid="job-title">{job.title || job.job_id}</h1>
          <div className="chips">
            <StateBadge state={job.state} />
            {job.qa && <RiskBadge risk={job.qa.risk_score} />}
            {job.lane !== "standard" && <Badge tone={job.lane === "fast" ? "ok" : "warn"} title={job.lane_reason}>{LANE_LABEL[job.lane]}</Badge>}
            <span className="mono faint small">{job.channel_id} · v{job.manifest_version} · {job.content_hash.slice(0, 15)}…</span>
          </div>
        </div>
      </div>
      {job.lane_reason && <div className="muted small" data-testid="lane-reason">{job.lane_reason}</div>}
      {error && <Notice tone="err">{error}</Notice>}
      {blocking && <Notice tone="warn">The quality check found a blocking issue. Reject it, or edit it and let the check run again.</Notice>}

      <div className="split">
        <div className="stack">
          <div className="media">
            {job.video_url ? <video controls src={job.video_url} data-testid="video" /> : job.thumbnail_url ? <img className="thumb" src={job.thumbnail_url} alt="Thumbnail" /> : <div className="placeholder">No preview available</div>}
          </div>
          {!wide && decision}
          {mode === "edit" && decide && (
            <form
              className="card card-pad stack"
              onSubmit={(e) => {
                e.preventDefault();
                const tags = edit.tags.split(",").map((t) => t.trim()).filter(Boolean);
                const changes: Record<string, unknown> = {};
                if (edit.title !== job.title) changes.title = edit.title;
                if (edit.description !== job.description) changes.description = edit.description;
                if (tags.join(",") !== job.tags.join(",")) changes.tags = tags;
                if (edit.script !== job.script) changes.script = edit.script;
                if (!Object.keys(changes).length) return setError("Nothing changed.");
                void act(() => api.edit(job.job_id, { content_hash: job.content_hash, ...changes }), "edited");
              }}
            >
              <Notice tone="warn">Saving creates a new version and sends it back to the quality check. It does not approve anything.</Notice>
              <Field label="Title">{(fid) => <input id={fid} name="title" className="input" value={edit.title} maxLength={100} onChange={(e) => setEdit({ ...edit, title: e.target.value })} />}</Field>
              <Field label="Description">{(fid) => <textarea id={fid} name="description" className="textarea" value={edit.description} onChange={(e) => setEdit({ ...edit, description: e.target.value })} />}</Field>
              <Field label="Tags (comma separated)">{(fid) => <input id={fid} name="tags" className="input" value={edit.tags} onChange={(e) => setEdit({ ...edit, tags: e.target.value })} />}</Field>
              <Field label="Script">{(fid) => <textarea id={fid} name="script" className="textarea" style={{ minHeight: 180 }} value={edit.script} onChange={(e) => setEdit({ ...edit, script: e.target.value })} />}</Field>
              <div className="row">
                <Button variant="primary" busy={busy} type="submit" data-testid="save-edit">Save edit</Button>
                <Button type="button" onClick={() => setMode("view")}>Cancel</Button>
              </div>
            </form>
          )}
          {mode === "reject" && decide && (
            <form
              className="card card-pad stack"
              onSubmit={(e) => {
                e.preventDefault();
                void act(() => api.reject(job.job_id, { content_hash: job.content_hash, ...reject }), "rejected");
              }}
            >
              <Field label="Reason (at least 10 characters)">
                {(fid) => <textarea id={fid} name="reason" className="textarea" value={reject.reason} minLength={10} required onChange={(e) => setReject({ ...reject, reason: e.target.value })} />}
              </Field>
              <Field label="Rework from">
                {(fid) => (
                  <select id={fid} name="rework_stage" className="select" value={reject.rework_stage} onChange={(e) => setReject({ ...reject, rework_stage: e.target.value })}>
                    <option value="research">Research</option>
                    <option value="script">Script</option>
                    <option value="render">Render</option>
                    <option value="discard">Discard (no rework)</option>
                  </select>
                )}
              </Field>
              <div className="row">
                <Button variant="danger" className="solid" busy={busy} disabled={reject.reason.trim().length < 10} type="submit" data-testid="confirm-reject">Reject</Button>
                <Button type="button" onClick={() => setMode("view")}>Cancel</Button>
              </div>
            </form>
          )}
          <Card title="Description"><div style={{ whiteSpace: "pre-wrap" }}>{job.description || <span className="muted">None</span>}</div>
            <div className="chips" style={{ marginTop: 10 }}>{job.tags.map((t) => <Badge key={t}>{t}</Badge>)}</div></Card>
          <Tabs<Tab> value={tab} onChange={setTab} tabs={[{ value: "script", label: "Script" }, { value: "checks", label: `Checks (${job.qa?.flags.length ?? 0})` }, { value: "history", label: "History" }]} />
          {tab === "script" && (
            <Card><div className="script" style={{ whiteSpace: "pre-wrap" }}>{job.script || <span className="muted">No script text in the manifest.</span>}</div>
              {prev && (
                <div className="stack" style={{ marginTop: 14 }} data-testid="diff">
                  <h3>Changes since version {job.previous!.version}</h3>
                  {(["title", "description", "tags", "script"] as const).map((k) => <Diff key={k} label={k} before={asText(prev[k])} after={asText(job.metadata[k])} />)}
                </div>
              )}
            </Card>
          )}
          {tab === "checks" && (
            <Card>
              {job.qa && !job.qa.for_current_content && <Notice tone="warn">The quality report is for an older version.</Notice>}
              <div className="stack" data-testid="flags">
                {job.qa?.flags.length ? job.qa.flags.map((f, i) => (
                  <div key={i} className={`flag ${asText(f.severity) === "block" ? "block" : ""}`}>
                    <div className="strong">{asText(f.claim) || asText(f.code)}</div>
                    <div className="muted small">{asText(f.reason) || asText(f.detail)}</div>
                  </div>
                )) : <span className="muted">Nothing flagged.</span>}
              </div>
              <h3 style={{ marginTop: 14 }}>Sources</h3>
              <div className="stack-sm" data-testid="sources">
                {job.qa?.sources.length ? job.qa.sources.map((s, i) => {
                  const href = safeHref(s.url);
                  const label = asText(s.title) || asText(s.url);
                  return <div key={i}>{href ? <a href={href} target="_blank" rel="noopener noreferrer nofollow">{label}</a> : label}<div className="mono faint small">{asText(s.url)}</div></div>;
                }) : <span className="muted">No sources.</span>}
              </div>
            </Card>
          )}
          {tab === "history" && (
            <Card>
              <div className="timeline">
                {events.map((ev) => (
                  <div className="tl-item" key={ev.id}>
                    <span className="tl-dot" />
                    <div className="strong">{STATE_LABEL[ev.to_state] ?? ev.to_state}</div>
                    <div className="faint small">{ev.actor} · {dateTime(ev.at)}</div>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
        <aside className="sticky-side stack">
          {wide && decision}
          <Card title="Details">
            <div className="stack-sm small">
              <div><span className="muted">Channel</span> · {job.channel_id}</div>
              <div><span className="muted">Version</span> · {job.manifest_version}</div>
              <div><span className="muted">Category</span> · {asText(job.metadata.category) || "-"}</div>
              <div><span className="muted">Privacy</span> · {asText(job.metadata.privacy_status) || "-"}</div>
            </div>
          </Card>
        </aside>
      </div>
    </div>
  );
}

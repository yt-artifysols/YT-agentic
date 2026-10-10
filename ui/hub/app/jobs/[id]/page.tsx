"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type Preview } from "@/lib/api";
import { asText, diffLines } from "@/lib/diff";
import { useMe } from "@/lib/useMe";
import { RiskBadge } from "../../RiskBadge";

type Mode = "view" | "edit" | "reject";

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
    <div style={{ marginBottom: 12 }}>
      <div className="muted" style={{ fontSize: 12 }}>{label}</div>
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

export default function JobPage() {
  const me = useMe();
  const router = useRouter();
  const { id } = useParams<{ id: string }>();
  const [job, setJob] = useState<Preview | null>(null);
  const [mode, setMode] = useState<Mode>("view");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [edit, setEdit] = useState({ title: "", description: "", tags: "", script: "" });
  const [reject, setReject] = useState({ reason: "", rework_stage: "script" });

  const load = useCallback(async () => {
    const p = await api.preview(id);
    setJob(p);
    setEdit({ title: p.title, description: p.description, tags: p.tags.join(", "), script: p.script });
  }, [id]);

  useEffect(() => {
    if (me) load().catch((e: Error) => setError(e.message));
  }, [me, load]);

  const act = useCallback(
    async (fn: () => Promise<unknown>, done: string) => {
      setBusy(true);
      setError(null);
      try {
        await fn();
        router.push(`/?done=${encodeURIComponent(done)}`);
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          setError("This video changed since you opened it. The latest version is shown below; review it again.");
          setMode("view");
          await load();
        } else setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [router, load],
  );

  const canDecide = me?.role === "approver" && job?.state === "awaiting_approval";

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLSelectElement;
      if (e.key === "Escape") {
        if (mode !== "view") setMode("view");
        else router.push("/");
        return;
      }
      if (typing || !canDecide || !job || busy) return;
      if (e.key === "a") void act(() => api.approve(job.job_id, job.content_hash), "approved");
      else if (e.key === "e") setMode("edit");
      else if (e.key === "r") setMode("reject");
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, canDecide, job, busy, act, router]);

  if (!me || !job) return error ? <div className="notice err">{error}</div> : null;
  const prev = job.previous?.metadata;

  return (
    <>
      <p><Link href="/">← Queue</Link></p>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 data-testid="job-title">{job.title || job.job_id}</h1>
        <div className="row">
          {job.qa && <RiskBadge risk={job.qa.risk_score} />}
          {job.fast_lane && <span className="badge risk-low">fast lane</span>}
          <span className="badge">{job.state}</span>
        </div>
      </div>
      <p className="mono muted">
        {job.channel_id} · version {job.manifest_version} · {job.content_hash.slice(0, 19)}…
      </p>
      {error && <div className="notice err" role="alert">{error}</div>}

      {canDecide && (
        <div className="card row" style={{ marginBottom: 16 }}>
          <button className="primary" disabled={busy} data-testid="approve"
            onClick={() => void act(() => api.approve(job.job_id, job.content_hash), "approved")}>
            Approve <span className="kbd">a</span>
          </button>
          <button disabled={busy} onClick={() => setMode("edit")} data-testid="edit">Edit <span className="kbd">e</span></button>
          <button className="danger" disabled={busy} onClick={() => setMode("reject")} data-testid="reject">
            Reject <span className="kbd">r</span>
          </button>
          <span className="muted" style={{ fontSize: 13 }}>Approving signs a token for exactly this version.</span>
        </div>
      )}

      {mode === "edit" && canDecide && (
        <form className="card" style={{ display: "grid", gap: 10, marginBottom: 16 }}
          onSubmit={(e) => {
            e.preventDefault();
            const tags = edit.tags.split(",").map((t) => t.trim()).filter(Boolean);
            const changes: Record<string, unknown> = {};
            if (edit.title !== job.title) changes.title = edit.title;
            if (edit.description !== job.description) changes.description = edit.description;
            if (tags.join(",") !== job.tags.join(",")) changes.tags = tags;
            if (edit.script !== job.script) changes.script = edit.script;
            if (!Object.keys(changes).length) return setError("Nothing changed.");
            void act(() => api.edit(job.job_id, { content_hash: job.content_hash, ...changes }), "edited; QA re-runs");
          }}>
          <div className="notice warn">Saving an edit creates a new version and sends it back to QA. It does not approve anything.</div>
          <label>Title<input name="title" value={edit.title} maxLength={100} onChange={(e) => setEdit({ ...edit, title: e.target.value })} /></label>
          <label>Description<textarea name="description" value={edit.description} onChange={(e) => setEdit({ ...edit, description: e.target.value })} /></label>
          <label>Tags (comma separated)<input name="tags" value={edit.tags} onChange={(e) => setEdit({ ...edit, tags: e.target.value })} /></label>
          <label>Script<textarea name="script" value={edit.script} style={{ minHeight: 160 }} onChange={(e) => setEdit({ ...edit, script: e.target.value })} /></label>
          <div className="row"><button className="primary" disabled={busy} type="submit" data-testid="save-edit">Save edit</button>
            <button type="button" onClick={() => setMode("view")}>Cancel <span className="kbd">Esc</span></button></div>
        </form>
      )}

      {mode === "reject" && canDecide && (
        <form className="card" style={{ display: "grid", gap: 10, marginBottom: 16 }}
          onSubmit={(e) => {
            e.preventDefault();
            void act(() => api.reject(job.job_id, { content_hash: job.content_hash, ...reject }), "rejected");
          }}>
          <label>Reason (at least 10 characters)
            <textarea name="reason" value={reject.reason} minLength={10} required onChange={(e) => setReject({ ...reject, reason: e.target.value })} />
          </label>
          <label>Rework from
            <select name="rework_stage" value={reject.rework_stage} onChange={(e) => setReject({ ...reject, rework_stage: e.target.value })}>
              <option value="research">Research</option>
              <option value="script">Script</option>
              <option value="render">Render</option>
              <option value="discard">Discard (no rework)</option>
            </select>
          </label>
          <div className="row"><button className="danger" disabled={busy || reject.reason.trim().length < 10} type="submit" data-testid="confirm-reject">Reject</button>
            <button type="button" onClick={() => setMode("view")}>Cancel <span className="kbd">Esc</span></button></div>
        </form>
      )}

      <div className="grid2">
        <section>
          {job.video_url && <video controls src={job.video_url} data-testid="video" />}
          {job.thumbnail_url && <img className="thumb" src={job.thumbnail_url} alt="Thumbnail" />}
          <h2>Description</h2>
          <div className="card" style={{ whiteSpace: "pre-wrap" }}>{job.description || <span className="muted">None</span>}</div>
          <h2>Tags</h2>
          <div className="card">{job.tags.length ? job.tags.join(", ") : <span className="muted">None</span>}</div>
          <h2>Script</h2>
          <div className="card" style={{ whiteSpace: "pre-wrap" }}>{job.script || <span className="muted">No script text in the manifest.</span>}</div>
          {prev && (
            <>
              <h2>Changes since version {job.previous!.version}</h2>
              <div className="card" data-testid="diff">
                {(["title", "description", "tags", "script"] as const).map((k) => (
                  <Diff key={k} label={k} before={asText(prev[k])} after={asText(job.metadata[k])} />
                ))}
              </div>
            </>
          )}
        </section>
        <aside>
          <h2>QA</h2>
          <div className="card">
            {job.qa ? (
              <>
                <div className="row"><RiskBadge risk={job.qa.risk_score} /> {job.qa.passed ? "passed" : <strong>failed</strong>}</div>
                {!job.qa.for_current_content && <div className="notice warn">QA report is for an older version.</div>}
              </>
            ) : <span className="muted">No QA report.</span>}
          </div>
          <h2>Flagged claims</h2>
          <div className="card" data-testid="flags">
            {job.qa?.flags.length ? (
              <ul className="plain">
                {job.qa.flags.map((f, i) => (
                  <li key={i}><strong>{asText(f.claim)}</strong><div className="muted">{asText(f.reason)}</div></li>
                ))}
              </ul>
            ) : <span className="muted">None flagged.</span>}
          </div>
          <h2>Sources</h2>
          <div className="card" data-testid="sources">
            {job.qa?.sources.length ? (
              <ul className="plain">
                {job.qa.sources.map((s, i) => {
                  const href = safeHref(s.url);
                  const label = asText(s.title) || asText(s.url);
                  return <li key={i}>{href ? <a href={href} target="_blank" rel="noopener noreferrer nofollow">{label}</a> : label}
                    <div className="mono muted">{asText(s.url)}</div></li>;
                })}
              </ul>
            ) : <span className="muted">No sources.</span>}
          </div>
        </aside>
      </div>
    </>
  );
}

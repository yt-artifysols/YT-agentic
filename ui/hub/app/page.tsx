"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, type QueueItem } from "@/lib/api";
import { useMe } from "@/lib/useMe";
import { RiskBadge } from "./RiskBadge";

const EMPTY = { cluster: "", channel: "", min_risk: "", max_risk: "", fast_lane: "", sort: "risk" };

export default function QueuePage() {
  const me = useMe();
  const router = useRouter();
  const [filters, setFilters] = useState(EMPTY);
  const [items, setItems] = useState<QueueItem[]>([]);
  const [cursor, setCursor] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const load = useCallback(() => {
    api.queue(filters).then((rows) => {
      setItems(rows);
      setSelected((sel) => new Set([...sel].filter((id) => rows.some((r) => r.job_id === id && r.fast_lane))));
    }).catch((e: Error) => setMessage({ kind: "err", text: e.message }));
  }, [filters]);

  useEffect(() => {
    if (me) load();
  }, [me, load]);

  const batch = useCallback(async () => {
    const chosen = items.filter((i) => selected.has(i.job_id));
    if (!chosen.length) return;
    try {
      const { results } = await api.batchApprove(chosen.map((i) => ({ job_id: i.job_id, content_hash: i.content_hash })));
      const failed = results.filter((r) => !r.ok);
      setMessage(failed.length
        ? { kind: "err", text: `${results.length - failed.length} approved; ${failed.length} refused: ${failed.map((f) => f.error).join("; ")}` }
        : { kind: "ok", text: `${results.length} video(s) approved` });
      setSelected(new Set());
      load();
    } catch (e) {
      setMessage({ kind: "err", text: (e as Error).message });
    }
  }, [items, selected, load]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
      const current = items[cursor];
      if (e.key === "j") setCursor((c) => Math.min(c + 1, items.length - 1));
      else if (e.key === "k") setCursor((c) => Math.max(c - 1, 0));
      else if ((e.key === "Enter" || e.key === "o") && current) router.push(`/jobs/${current.job_id}`);
      else if (e.key === "x" && current?.fast_lane)
        setSelected((s) => {
          const n = new Set(s);
          n.has(current.job_id) ? n.delete(current.job_id) : n.add(current.job_id);
          return n;
        });
      else if (e.key === "A" && e.shiftKey && me?.role === "approver") void batch();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, cursor, router, batch, me]);

  if (!me) return null;
  const set = (k: keyof typeof EMPTY) => (e: { target: { value: string } }) => setFilters({ ...filters, [k]: e.target.value });

  return (
    <>
      <h1>Waiting for approval</h1>
      <div className="card row" style={{ marginBottom: 12 }}>
        <label>Cluster<input value={filters.cluster} onChange={set("cluster")} size={10} /></label>
        <label>Channel<input value={filters.channel} onChange={set("channel")} size={12} /></label>
        <label>Min risk<input type="number" min={0} max={100} value={filters.min_risk} onChange={set("min_risk")} style={{ width: 80 }} /></label>
        <label>Max risk<input type="number" min={0} max={100} value={filters.max_risk} onChange={set("max_risk")} style={{ width: 80 }} /></label>
        <label>Lane
          <select value={filters.fast_lane} onChange={set("fast_lane")}>
            <option value="">All</option>
            <option value="true">Fast lane only</option>
          </select>
        </label>
        <label>Sort
          <select value={filters.sort} onChange={set("sort")}>
            <option value="risk">Highest risk first</option>
            <option value="deadline">Nearest deadline</option>
            <option value="channel">Channel</option>
            <option value="cluster">Cluster</option>
          </select>
        </label>
        <button onClick={load}>Refresh</button>
        {me.role === "approver" && (
          <button className="primary" disabled={!selected.size} onClick={() => void batch()} data-testid="batch-approve">
            Approve {selected.size || ""} selected (fast lane)
          </button>
        )}
      </div>
      {message && <div className={`notice ${message.kind}`} role="status">{message.text}</div>}
      <div className="card" style={{ padding: 0, overflowX: "auto" }}>
        <table>
          <thead>
            <tr><th /><th>Risk</th><th>Title</th><th>Channel</th><th>Origin</th><th>Flags</th><th>Deadline</th></tr>
          </thead>
          <tbody>
            {items.map((item, i) => (
              <tr key={item.job_id} className={i === cursor ? "sel" : undefined} data-testid="queue-row">
                <td>
                  <input type="checkbox" aria-label="select for batch approval" disabled={!item.fast_lane}
                    checked={selected.has(item.job_id)}
                    onChange={() => setSelected((s) => {
                      const n = new Set(s);
                      n.has(item.job_id) ? n.delete(item.job_id) : n.add(item.job_id);
                      return n;
                    })} />
                </td>
                <td><RiskBadge risk={item.risk_score} /></td>
                <td><Link href={`/jobs/${item.job_id}`}>{item.title || item.job_id}</Link>
                  {item.fast_lane && <span className="badge risk-low" style={{ marginLeft: 6 }} title={item.lane_reason}>fast lane</span>}
                  {item.lane === "deep" && <span className="badge risk-high" style={{ marginLeft: 6 }} title={item.lane_reason}>deep review</span>}</td>
                <td className="mono">{item.channel_id}</td>
                <td>{item.origin}</td>
                <td>{item.flag_count}</td>
                <td className="mono">{item.approval_deadline ? new Date(item.approval_deadline).toLocaleString() : "-"}</td>
              </tr>
            ))}
            {!items.length && (
              <tr><td colSpan={7} className="muted">Nothing waiting.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ fontSize: 13 }}>
        <span className="kbd">j</span>/<span className="kbd">k</span> move · <span className="kbd">Enter</span> open ·{" "}
        <span className="kbd">x</span> select fast-lane item · <span className="kbd">Shift+A</span> approve selected
      </p>
    </>
  );
}

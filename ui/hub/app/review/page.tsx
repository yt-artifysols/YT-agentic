"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { Badge, Button, Empty, Notice, PageHead, RiskBadge, Segmented, SkeletonList, useToast } from "@/components/ui";
import { api, type QueueItem } from "@/lib/api";
import { LANE_LABEL, countdown } from "@/lib/format";
import { canDecide, useSession } from "@/lib/session";
import { useApi } from "@/lib/useApi";

type Lane = "all" | "standard" | "fast" | "deep";

export default function ReviewPage() {
  const { me, refresh } = useSession();
  const toast = useToast();
  const [lane, setLane] = useState<Lane>("all");
  const [sort, setSort] = useState("risk");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const { data, error, loading, reload } = useApi(() => api.queue({ sort }), [sort], 30_000);

  const items = useMemo(() => (data ?? []).filter((i) => lane === "all" || i.lane === lane), [data, lane]);
  const fastIds = items.filter((i) => i.lane === "fast" && i.qa_passed && i.flag_count === 0);
  const toggle = (id: string) => setPicked((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  const batch = async () => {
    const chosen = items.filter((i) => picked.has(i.job_id));
    setBusy(true);
    try {
      const res = await api.batchApprove(chosen.map((i) => ({ job_id: i.job_id, content_hash: i.content_hash })));
      const ok = res.results.filter((r) => r.ok).length;
      toast(ok === chosen.length ? "ok" : "err", `${ok} of ${chosen.length} approved`);
      setPicked(new Set());
      await reload();
      await refresh();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <PageHead title="Review" sub="Open a video to inspect it. Approval signs exactly the version you saw." />
      <Segmented<Lane>
        label="Lane"
        value={lane}
        onChange={setLane}
        options={[{ value: "all", label: "All" }, { value: "standard", label: "Review" }, { value: "fast", label: "Fast lane" }, { value: "deep", label: "Deep review" }]}
      />
      <div className="row">
        <label className="small muted" htmlFor="sort">Sort</label>
        <select id="sort" className="select" style={{ width: "auto" }} value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="risk">Highest risk first</option>
          <option value="deadline">Soonest deadline</option>
        </select>
      </div>
      {error && <Notice tone="err">{error}</Notice>}
      {loading && !data && <SkeletonList />}
      {data && items.length === 0 && <Empty title="Queue is clear">No videos are waiting in this lane.</Empty>}
      <div className="list card">
        {items.map((i) => (
          <Row key={i.job_id} item={i} selectable={canDecide(me) && i.lane === "fast" && fastIds.includes(i)} checked={picked.has(i.job_id)} onToggle={() => toggle(i.job_id)} />
        ))}
      </div>
      {picked.size > 0 && (
        <div className="actionbar">
          <span className="strong">{picked.size} selected</span>
          <Button variant="primary" busy={busy} icon="check" onClick={() => void batch()}>Approve selected (passkey)</Button>
        </div>
      )}
    </div>
  );
}

function Row({ item, selectable, checked, onToggle }: { item: QueueItem; selectable: boolean; checked: boolean; onToggle: () => void }) {
  const cd = countdown(item.approval_deadline);
  return (
    <div className="item" data-testid="queue-row">
      {selectable && <input type="checkbox" aria-label={`Select ${item.title}`} checked={checked} onChange={onToggle} />}
      <Link href={`/jobs/${item.job_id}`} className="item-main">
        <div className="item-title">{item.title || item.job_id}</div>
        <div className="item-sub">{item.channel_id} · <span className={cd.tone === "bad" ? "strong" : ""}>{cd.text}</span> · {item.flag_count} flag(s)</div>
      </Link>
      <div className="chips">
        <RiskBadge risk={item.risk_score} />
        {item.lane !== "standard" && <Badge tone={item.lane === "fast" ? "ok" : "warn"} title={item.lane_reason}>{LANE_LABEL[item.lane]}</Badge>}
      </div>
    </div>
  );
}

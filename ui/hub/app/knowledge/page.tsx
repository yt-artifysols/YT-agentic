"use client";

import { useState } from "react";
import { Badge, Button, Card, Empty, Field, Modal, Notice, PageHead, SkeletonList, useToast } from "@/components/ui";
import { api, type KnowledgeDraft } from "@/lib/api";
import { relTime } from "@/lib/format";
import { canDecide, useSession } from "@/lib/session";
import { useApi } from "@/lib/useApi";

export default function KnowledgePage() {
  const { me } = useSession();
  const toast = useToast();
  const { data, error, loading, reload } = useApi(() => api.drafts(), []);
  const [open, setOpen] = useState<KnowledgeDraft | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const view = async (id: string) => setOpen(await api.draft(id));
  const decide = async (kind: "approve" | "reject") => {
    if (!open) return;
    setBusy(true);
    try {
      if (kind === "approve") await api.approveDraft(open.id, open.content_sha256);
      else await api.rejectDraft(open.id, open.content_sha256, reason);
      toast("ok", kind === "approve" ? "Approved. This is now the channel's active version." : "Rejected.");
      setOpen(null);
      setReason("");
      await reload();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <PageHead title="Brand & policy" sub="Agents can only propose changes. Nothing takes effect until a person approves it here." />
      {error && <Notice tone="err">{error}</Notice>}
      {loading && !data && <SkeletonList />}
      {data?.length === 0 && <Empty title="No proposals waiting" />}
      <div className="list card">
        {data?.map((d) => (
          <button key={d.id} className="item" style={{ textAlign: "left", background: "none", border: 0, width: "100%", cursor: "pointer" }} onClick={() => void view(d.id)}>
            <div className="item-main">
              <div className="item-title">{d.kind.replace(/_/g, " ")} · {d.channel_id}</div>
              <div className="item-sub">v{d.version} proposed by {d.proposed_by} · {relTime(d.proposed_at)}</div>
            </div>
            <Badge tone="warn">Needs review</Badge>
          </button>
        ))}
      </div>
      {open && (
        <Modal title={`${open.kind.replace(/_/g, " ")} · ${open.channel_id}`} onClose={() => setOpen(null)}>
          {open.stale && <Notice tone="warn">The active version moved on since this was proposed. Reject it and ask for a fresh proposal.</Notice>}
          <pre className="diff">
            {open.diff.map((l, i) => (
              <span key={i} className={l.op === "same" ? undefined : l.op}>{l.op === "add" ? "+ " : l.op === "del" ? "- " : "  "}{l.text}{"\n"}</span>
            ))}
          </pre>
          {canDecide(me) || me?.role === "admin" ? (
            <div className="stack">
              <Field label="Reason (needed to reject)">{(id) => <input id={id} className="input" value={reason} onChange={(e) => setReason(e.target.value)} />}</Field>
              <div className="row">
                <Button variant="success" icon="check" busy={busy} disabled={open.stale} onClick={() => void decide("approve")}>Approve with passkey</Button>
                <Button variant="danger" busy={busy} disabled={reason.trim().length < 5} onClick={() => void decide("reject")}>Reject</Button>
              </div>
            </div>
          ) : <Notice tone="info">Only approvers and admins can decide on proposals.</Notice>}
        </Modal>
      )}
      <Card title="How this works"><div className="muted small">Each proposal shows exactly what changes. Your approval is bound to the exact text you see.</div></Card>
    </div>
  );
}

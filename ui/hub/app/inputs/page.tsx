"use client";

import { useState } from "react";
import { Badge, Button, Card, Empty, Field, Notice, PageHead, SkeletonList, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { relTime } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export default function InputsPage() {
  const toast = useToast();
  const channels = useApi(() => api.channels(), []);
  const [channel, setChannel] = useState("");
  const current = channel || channels.data?.[0]?.id || "";
  const list = useApi(() => (current ? api.inputs(current) : Promise.resolve([])), [current]);
  const [kind, setKind] = useState("idea");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  const add = async () => {
    setBusy(true);
    try {
      await api.addInput(current, { kind, body: text.trim() });
      setText("");
      toast("ok", "Saved. Inputs are treated as data, never as instructions.");
      await list.reload();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <PageHead title="Ideas & inputs" sub="Tell a channel what to make. Agents use these as source material; you still approve every video." />
      <Field label="Channel">
        {(id) => (
          <select id={id} className="select" value={current} onChange={(e) => setChannel(e.target.value)}>
            {channels.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        )}
      </Field>
      <Card title="Add an input">
        <div className="stack">
          <Field label="Type">
            {(id) => (
              <select id={id} className="select" value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="idea">Idea</option><option value="goal">Goal</option><option value="note">Note</option><option value="reference">Reference</option>
              </select>
            )}
          </Field>
          <Field label="What should the channel know or make?">
            {(id) => <textarea id={id} className="textarea" value={text} onChange={(e) => setText(e.target.value)} placeholder="e.g. Explain why savings accounts pay so little" />}
          </Field>
          <Button variant="primary" icon="plus" busy={busy} disabled={!text.trim() || !current} onClick={() => void add()}>Add</Button>
        </div>
      </Card>
      {list.error && <Notice tone="err">{list.error}</Notice>}
      {list.loading && !list.data && <SkeletonList />}
      {list.data?.length === 0 && <Empty icon="idea" title="No inputs yet" />}
      <div className="list card">
        {list.data?.map((i) => (
          <div key={i.id} className="item">
            <div className="item-main">
              <div className="item-title">{i.body || i.uri}</div>
              <div className="item-sub">{i.kind} · {i.submitted_by} · {relTime(i.created_at)}</div>
            </div>
            <Badge tone={i.status === "new" ? "brand" : ""}>{i.status === "new" ? "Waiting" : "Used"}</Badge>
          </div>
        ))}
      </div>
    </div>
  );
}

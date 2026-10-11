"use client";

import { useState } from "react";
import { Badge, Button, Card, Field, Notice, PageHead, SkeletonList, useConfirm, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { isStaff, useSession } from "@/lib/session";
import { useApi } from "@/lib/useApi";

export default function ControlsPage() {
  const { me, refresh } = useSession();
  const toast = useToast();
  const { ask, dialog } = useConfirm();
  const { data, error, reload } = useApi(() => api.controls(), []);
  const [reason, setReason] = useState("");
  if (error && !data) return <Notice tone="err">{error}</Notice>;
  if (!data) return <SkeletonList />;
  const canAct = isStaff(me);

  const flip = async (label: string, pause: boolean, fn: () => Promise<unknown>) => {
    if (pause && !(await ask(`Pause publishing for ${label}?`, "Nothing will be published until it is resumed. Videos in production keep going.", { confirm: "Pause", danger: true }))) return;
    try {
      await fn();
      toast("ok", `${label}: publishing ${pause ? "paused" : "resumed"}`);
      await reload();
      await refresh();
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  const row = (key: string, label: string, sub: string, paused: boolean, testid: string, fn: (p: boolean) => Promise<unknown>) => (
    <div className="item" key={key}>
      <div className="item-main"><div className="item-title">{label}</div><div className="item-sub">{sub}</div></div>
      <Badge tone={paused ? "bad" : "ok"}>{paused ? "Paused" : "Active"}</Badge>
      <Button size="small" variant={paused ? "primary" : "danger"} disabled={!canAct} data-testid={testid} onClick={() => void flip(label, !paused, () => fn(!paused))}>
        {paused ? "Resume" : "Pause"}
      </Button>
    </div>
  );

  return (
    <div className="stack">
      <PageHead title="Kill switches" sub="Stop publishing instantly. Stopping is one tap; resuming needs your passkey." />
      {!canAct && <Notice tone="warn">Only operators and admins can change kill switches.</Notice>}
      <Field label="Reason (recorded in the audit log)">{(id) => <input id={id} className="input" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. strike warning on channel" />}</Field>
      <Card title="Everything" pad={false}><div className="list">{row("g", "All channels", "Stops all publishing", data.global_paused, "toggle-global", (p) => api.setGlobal(p, reason))}</div></Card>
      <Card title="Clusters" pad={false}><div className="list">{data.clusters.map((c) => row(c.id, c.name, c.id, c.status === "paused", `toggle-cluster-${c.id}`, (p) => api.setScoped("clusters", c.id, p, reason)))}</div></Card>
      <Card title="Channels" pad={false}><div className="list">{data.channels.map((c) => row(c.id, c.name, `${c.cluster_id ?? "no cluster"} · ${c.tier}`, c.status === "paused", `toggle-channel-${c.id}`, (p) => api.setScoped("channels", c.id, p, reason)))}</div></Card>
      {dialog}
    </div>
  );
}

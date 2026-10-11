"use client";

import { useState } from "react";
import { Badge, Button, Card, Field, Notice, PageHead, SkeletonList, useToast } from "@/components/ui";
import { api, type Role } from "@/lib/api";
import { relTime } from "@/lib/format";
import { useSession } from "@/lib/session";
import { useApi } from "@/lib/useApi";

export default function AdminPage() {
  const { me } = useSession();
  const toast = useToast();
  const { data, error, reload } = useApi(() => api.users(), []);
  const [form, setForm] = useState({ username: "", display_name: "", role: "approver" as Role });
  const [invite, setInvite] = useState<string | null>(null);
  if (me && me.role !== "admin") return <Notice tone="warn">Only admins can manage people.</Notice>;

  const send = async () => {
    try {
      const res = await api.invite(form);
      setInvite(res.invite_token);
      toast("ok", "Invite created. Share the code privately; it works once.");
      await reload();
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };

  return (
    <div className="stack">
      <PageHead title="People" sub="Everyone signs in with a passkey. Roles: approver (decides on videos), operator (kill switches), admin (all, plus invites)." />
      {error && <Notice tone="err">{error}</Notice>}
      {!data && <SkeletonList />}
      <Card title="Team" pad={false}>
        <div className="list">
          {data?.map((u) => (
            <div key={u.id} className="item">
              <div className="item-main"><div className="item-title">{u.display_name}</div><div className="item-sub">{u.username} · {u.passkeys} passkey(s) · last used {relTime(u.last_used_at)}</div></div>
              <Badge tone={u.disabled ? "bad" : "brand"}>{u.disabled ? "Disabled" : u.role}</Badge>
            </div>
          ))}
        </div>
      </Card>
      <Card title="Invite someone">
        <div className="stack">
          <Field label="Username">{(id) => <input id={id} className="input" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />}</Field>
          <Field label="Display name">{(id) => <input id={id} className="input" value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />}</Field>
          <Field label="Role">{(id) => (
            <select id={id} className="select" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
              <option value="approver">Approver</option><option value="operator">Operator</option><option value="admin">Admin</option>
            </select>)}</Field>
          <Button variant="primary" icon="plus" disabled={!form.username.trim() || !form.display_name.trim()} onClick={() => void send()}>Create invite (passkey)</Button>
          {invite && <Notice tone="ok">Invite code (shown once): <span className="mono">{invite}</span></Notice>}
        </div>
      </Card>
    </div>
  );
}

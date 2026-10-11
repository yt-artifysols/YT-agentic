"use client";

import { startAuthentication, startRegistration } from "@simplewebauthn/browser";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Icon } from "@/components/icons";
import { Button, Field, Notice } from "@/components/ui";
import { DEMO } from "@/lib/demo";
import { api, rememberCsrf } from "@/lib/api";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [username, setUsername] = useState("");
  const [invite, setInvite] = useState(params.get("invite") ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <div className="login-card stack">
        <div className="row"><div className="logo">YT</div><div><div className="brand-name" style={{ fontSize: 20 }}>Control room</div><div className="faint small">Every video needs your approval</div></div></div>
        {DEMO && <Notice tone="info">Demo preview: <a href="./">enter the sample console</a>. Real sign-in uses a passkey.</Notice>}
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              const options = await api.loginOptions(username.trim());
              const credential = await startAuthentication(options as never);
              rememberCsrf((await api.loginVerify(credential)).csrf_token);
            });
          }}
        >
          <Field label="Username">{(id) => <input id={id} name="username" className="input" autoComplete="username webauthn" value={username} onChange={(e) => setUsername(e.target.value)} required />}</Field>
          <Button variant="primary" className="block" icon="key" busy={busy} disabled={!username.trim()} type="submit">Sign in with passkey</Button>
        </form>
        <div className="hr" />
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              const options = await api.registerOptions(invite.trim());
              const credential = await startRegistration(options as never);
              rememberCsrf((await api.registerVerify(credential)).csrf_token);
            });
          }}
        >
          <Field label="First time? Invite code" hint="Your admin gives you a one-time code. You then create a passkey on this device.">{(id) => <input id={id} name="invite" className="input" value={invite} onChange={(e) => setInvite(e.target.value)} />}</Field>
          <Button className="block" icon="lock" busy={busy} disabled={!invite.trim()} type="submit">Register passkey</Button>
        </form>
        {error && <Notice tone="err">{error}</Notice>}
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}

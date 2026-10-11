"use client";

import { startAuthentication, startRegistration } from "@simplewebauthn/browser";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
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
    <div style={{ maxWidth: 420, margin: "48px auto", display: "grid", gap: 16 }}>
      <h1>Approval hub</h1>
      <form
        className="card"
        style={{ display: "grid", gap: 12 }}
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            const options = await api.loginOptions(username.trim());
            const credential = await startAuthentication(options as never);
            rememberCsrf((await api.loginVerify(credential)).csrf_token);
          });
        }}
      >
        <label>
          Username
          <input name="username" autoComplete="username webauthn" value={username}
            onChange={(e) => setUsername(e.target.value)} required />
        </label>
        <button className="primary" disabled={busy || !username.trim()} type="submit">
          Sign in with passkey
        </button>
      </form>
      <form
        className="card"
        style={{ display: "grid", gap: 12 }}
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            const options = await api.registerOptions(invite.trim());
            const credential = await startRegistration(options as never);
            rememberCsrf((await api.registerVerify(credential)).csrf_token);
          });
        }}
      >
        <label>
          First time? Invite code
          <input name="invite" value={invite} onChange={(e) => setInvite(e.target.value)} />
        </label>
        <button disabled={busy || !invite.trim()} type="submit">
          Register passkey
        </button>
      </form>
      {error && <div className="notice err" role="alert">{error}</div>}
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

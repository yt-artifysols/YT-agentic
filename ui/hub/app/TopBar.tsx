"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, forgetCsrf, type Me } from "@/lib/api";

export function TopBar() {
  const path = usePathname();
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  useEffect(() => {
    if (path === "/login") return;
    api.me().then(setMe).catch(() => setMe(null));
  }, [path]);
  if (path === "/login") return null;
  return (
    <header className="top">
      <span className="brand">Approval hub</span>
      <nav>
        <Link href="/">Queue</Link>
        {me && <Link href="/clusters">Clusters</Link>}
        {me && <Link href="/scorecards">Scorecards</Link>}
        {me && me.role !== "approver" && <Link href="/controls">Kill switches</Link>}
      </nav>
      {me && (
        <span className="who" data-testid="whoami">
          {me.display_name} · {me.role}
        </span>
      )}
      {me && (
        <button
          onClick={async () => {
            await api.logout().catch(() => undefined);
            forgetCsrf();
            router.replace("/login");
          }}
        >
          Sign out
        </button>
      )}
    </header>
  );
}

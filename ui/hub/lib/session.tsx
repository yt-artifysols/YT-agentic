"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { ApiError, api, type Me, type Overview } from "./api";
import { useApi } from "./useApi";

interface Session {
  me: Me | null;
  overview: Overview | null;
  refresh: () => Promise<void>;
}
const Ctx = createContext<Session>({ me: null, overview: null, refresh: async () => undefined });
export const useSession = () => useContext(Ctx);

export function SessionProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const path = usePathname();
  const onLogin = path.startsWith("/login");
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
    if (onLogin) return;
    api
      .me()
      .then(setMe)
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 401) router.replace("/login");
      });
  }, [onLogin, router]);

  const ov = useApi(() => (me ? api.overview() : Promise.resolve(null as unknown as Overview)), [me?.username], me ? 30_000 : 0);
  return <Ctx.Provider value={{ me, overview: me ? ov.data : null, refresh: ov.reload }}>{children}</Ctx.Provider>;
}

export const canDecide = (me: Me | null) => me?.role === "approver";
export const isStaff = (me: Me | null) => me?.role === "operator" || me?.role === "admin";

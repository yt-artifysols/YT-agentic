"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => Promise<void>;
}

// Small data hook: loads on mount (and when deps change), optional polling, keeps the last data
// while reloading so lists do not flash empty.
export function useApi<T>(fetcher: () => Promise<T>, deps: unknown[] = [], pollMs = 0): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  const alive = useRef(true);

  const load = useCallback(async () => {
    try {
      const v = await fetchRef.current();
      if (alive.current) {
        setData(v);
        setError(null);
      }
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    setLoading(true);
    void load();
    const t = pollMs > 0 ? setInterval(() => void load(), pollMs) : undefined;
    return () => {
      alive.current = false;
      if (t) clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, pollMs, ...deps]);

  return { data, error, loading, reload: load };
}

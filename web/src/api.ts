// Thin fetch wrapper + a polling hook. Every call goes to the FastAPI app under /api.
import { useCallback, useEffect, useRef, useState } from "react";
import type { Card, CoverLetter, Gap, JobDetail, Ops, Resume, Stage } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

export const api = {
  jobs: () => call<Card[]>("/jobs"),
  job: (id: string) => call<JobDetail>(`/jobs/${encodeURIComponent(id)}`),
  evidence: () => call<Record<string, string>>("/evidence"),
  setStage: (id: string, stage: Stage) =>
    call(`/jobs/${encodeURIComponent(id)}/stage`, { method: "PATCH", body: JSON.stringify({ stage }) }),
  addJob: (body: { url?: string; text?: string; company?: string; title?: string; location?: string }) =>
    call<{ id: string }>("/jobs", { method: "POST", body: JSON.stringify(body) }),
  rescore: (id: string) => call(`/jobs/${encodeURIComponent(id)}/score`, { method: "POST" }),
  tailor: (id: string) => call(`/jobs/${encodeURIComponent(id)}/tailor`, { method: "POST" }),
  review: (
    id: string,
    body: { action: "approve" | "edit" | "revise"; resume?: Resume; cover_letter?: CoverLetter; note?: string },
  ) => call(`/jobs/${encodeURIComponent(id)}/review`, { method: "POST", body: JSON.stringify(body) }),
  gaps: (minFit: number, soft = false) => call<Gap[]>(`/gaps?min_fit=${minFit}&soft=${soft}`),
  ops: () => call<Ops>("/ops"),
  pdfUrl: (id: string, kind: "resume" | "cover") => `/api/jobs/${encodeURIComponent(id)}/pdf/${kind}`,
};

/** Load data; re-poll every `intervalMs` while `shouldPoll(data)` is true. */
export function useData<T>(
  load: () => Promise<T>,
  deps: unknown[],
  opts: { intervalMs?: number; shouldPoll?: (d: T) => boolean } = {},
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumping `kick` restarts the poll loop, e.g. right after the user starts a background task.
  const [kick, setKick] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const pollRef = useRef(opts.shouldPoll);
  pollRef.current = opts.shouldPoll;

  const reload = useCallback(async () => {
    try {
      const d = await loadRef.current();
      setData(d);
      setError(null);
      return d;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }, []);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      const d = await reload();
      if (alive && d && pollRef.current?.(d)) timer = setTimeout(tick, opts.intervalMs ?? 2500);
    };
    void tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, kick]);

  const restart = useCallback(() => setKick((k) => k + 1), []);
  return { data, error, reload, restart, setData };
}

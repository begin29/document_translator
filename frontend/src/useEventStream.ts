import { useEffect, useRef, useState } from "react";
import { DocumentMessage, JobMessage } from "./api";

const JOB_TERMINAL = new Set(["done", "failed", "cancelled"]);
const DOCUMENT_TERMINAL = new Set(["ready", "failed"]);
const STALE_MS = 30_000;
const POLL_MS = 5_000;
const REBUILD_MS = 500;

export type StreamMessage = JobMessage | DocumentMessage;

type StreamMode = "stream" | "polling";

export type StreamHandlers = {
  fetchState: () => Promise<void> | void;
  onMessage: (msg: StreamMessage) => void;
};

type OpenHandlers = StreamHandlers & {
  onMode?: (mode: StreamMode) => void;
};

export type StreamSession = {
  mode: StreamMode;
  close: () => void;
};

type StateFields = {
  seq?: number;
  status?: string;
  analysis_status?: string;
  done_segments?: number;
  total_segments?: number;
  cost_cents?: number;
  error_code?: string | null;
};

export function isTerminal(kind: "job" | "document", status: string | null | undefined): boolean {
  if (!status) return false;
  return (kind === "document" ? DOCUMENT_TERMINAL : JOB_TERMINAL).has(status);
}

export function eventsUrl(ids: { jobs: string[]; documents: string[] }): string {
  const params = new URLSearchParams();
  for (const id of [...ids.jobs].sort()) params.append("job", id);
  for (const id of [...ids.documents].sort()) params.append("document", id);
  const query = params.toString();
  return query ? `/events?${query}` : "/events";
}

export function mergeState<T extends StateFields>(current: T, msg: StreamMessage): T {
  const kind = msg.type;
  const nextStatus = msg.type === "job" ? msg.status : msg.analysis_status;
  const previousStatus = msg.type === "job" ? current.status : current.analysis_status;
  const nextTerminal = isTerminal(kind, nextStatus);
  const previousTerminal = isTerminal(kind, previousStatus);
  if (previousTerminal && !nextTerminal) return current;
  const currentSeq = current.seq ?? 0;
  if (msg.seq <= currentSeq && !(nextTerminal && !previousTerminal)) return current;
  const seq = Math.max(msg.seq, currentSeq);
  if (msg.type === "job") {
    return {
      ...current,
      status: msg.status,
      done_segments: msg.done_segments,
      total_segments: msg.total_segments,
      cost_cents: msg.cost_cents,
      error_code: msg.error_code,
      seq,
    };
  }
  return {
    ...current,
    analysis_status: msg.analysis_status,
    error_code: msg.error_code,
    seq,
  };
}

export function applySnapshot<T extends StateFields>(current: T | undefined, incoming: T, kind: "job" | "document"): T {
  if (!current) return incoming;
  const previous = kind === "job" ? current.status : current.analysis_status;
  const next = kind === "job" ? incoming.status : incoming.analysis_status;
  if (isTerminal(kind, previous) && !isTerminal(kind, next)) return current;
  const seq = Math.max(current.seq ?? 0, incoming.seq ?? 0);
  if (!seq) return incoming;
  return { ...incoming, seq };
}

function safeCall(work: () => Promise<void> | void) {
  try {
    const result = work();
    if (result && typeof result.then === "function") result.then(() => undefined, () => undefined);
  } catch {
    return;
  }
}

export function openEventStream(ids: { jobs: string[]; documents: string[] }, handlers: OpenHandlers): StreamSession {
  const session: StreamSession = {
    mode: "stream",
    close: () => undefined,
  };
  const jobs = [...ids.jobs];
  const documents = [...ids.documents];
  if (jobs.length + documents.length === 0) return session;

  const Source = globalThis.EventSource;
  const watched = [...jobs, ...documents];
  const seenAt = new Map<string, number>();
  const touch = (id?: string) => {
    const now = Date.now();
    if (id) {
      seenAt.set(id, now);
      return;
    }
    for (const item of watched) seenAt.set(item, now);
  };
  touch();

  let closed = false;
  let errors = 0;
  let source: EventSource | null = new Source(eventsUrl({ jobs, documents }));
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let staleTimer: ReturnType<typeof setInterval> | null = null;

  const setMode = (mode: StreamMode) => {
    session.mode = mode;
    handlers.onMode?.(mode);
  };

  const enterPolling = () => {
    if (closed || session.mode === "polling") return;
    setMode("polling");
    const current = source;
    source = null;
    current?.close();
    if (staleTimer) {
      clearInterval(staleTimer);
      staleTimer = null;
    }
    if (pollTimer) return;
    safeCall(handlers.fetchState);
    pollTimer = setInterval(() => safeCall(handlers.fetchState), POLL_MS);
  };

  source.addEventListener("subscribed", () => {
    errors = 0;
    touch();
    safeCall(handlers.fetchState);
  });
  source.addEventListener("resync", () => {
    errors = 0;
    touch();
    safeCall(handlers.fetchState);
  });
  source.addEventListener("update", (event) => {
    const raw = (event as MessageEvent).data;
    if (typeof raw !== "string") return;
    let msg: StreamMessage;
    try {
      msg = JSON.parse(raw) as StreamMessage;
    } catch {
      return;
    }
    errors = 0;
    touch(msg.id);
    handlers.onMessage(msg);
  });
  source.onerror = () => {
    if (closed || session.mode === "polling") return;
    errors += 1;
    if (errors >= 3) enterPolling();
  };

  staleTimer = setInterval(() => {
    if (closed || session.mode === "polling") return;
    const now = Date.now();
    const quiet = watched.some((id) => now - (seenAt.get(id) ?? 0) >= STALE_MS);
    if (!quiet) return;
    touch();
    safeCall(handlers.fetchState);
  }, POLL_MS);

  session.close = () => {
    closed = true;
    source?.close();
    source = null;
    if (pollTimer) clearInterval(pollTimer);
    if (staleTimer) clearInterval(staleTimer);
    pollTimer = null;
    staleTimer = null;
  };
  return session;
}

export function useEventStream(ids: { jobs: string[]; documents: string[] }, handlers: StreamHandlers): { mode: StreamMode } {
  const [mode, setMode] = useState<StreamMode>("stream");
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  const jobsKey = [...ids.jobs].sort().join("\n");
  const documentsKey = [...ids.documents].sort().join("\n");

  useEffect(() => {
    if (typeof document === "undefined") return;
    const onVisible = () => {
      if (document.visibilityState === "visible") safeCall(() => handlersRef.current.fetchState());
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, []);

  useEffect(() => {
    const jobs = jobsKey ? jobsKey.split("\n") : [];
    const documents = documentsKey ? documentsKey.split("\n") : [];
    if (jobs.length + documents.length === 0) {
      setMode("stream");
      return;
    }
    let session: StreamSession | null = null;
    const timer = setTimeout(() => {
      setMode("stream");
      session = openEventStream(
        { jobs, documents },
        {
          fetchState: () => handlersRef.current.fetchState(),
          onMessage: (msg) => handlersRef.current.onMessage(msg),
          onMode: setMode,
        },
      );
    }, REBUILD_MS);
    return () => {
      clearTimeout(timer);
      session?.close();
    };
  }, [jobsKey, documentsKey]);

  return { mode };
}

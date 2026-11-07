import { useEffect, useRef, useState } from "react";
import { DocumentMessage, JobMessage, request } from "./api";

const JOB_TERMINAL = new Set(["done", "failed", "cancelled"]);
const DOCUMENT_TERMINAL = new Set(["ready", "failed"]);
const STALE_MS = 30_000;
const POLL_MS = 5_000;
const BACKOFF_START_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;

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

type Listener = OpenHandlers & {
  session: StreamSession;
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

const listeners = new Set<Listener>();
let source: EventSource | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let staleTimer: ReturnType<typeof setInterval> | null = null;
let connectTask: Promise<void> | null = null;
let generation = 0;
let backoff = BACKOFF_START_MS;
let lastActivity = 0;
let mode: StreamMode = "stream";

export function eventsUrl(ticket: string): string {
  return `/events?ticket=${encodeURIComponent(ticket)}`;
}

export function isTerminal(kind: "job" | "document", status: string | null | undefined): boolean {
  if (!status) return false;
  return (kind === "document" ? DOCUMENT_TERMINAL : JOB_TERMINAL).has(status);
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

function publishMode() {
  for (const listener of listeners) {
    listener.session.mode = mode;
    listener.onMode?.(mode);
  }
}

function touch() {
  lastActivity = Date.now();
}

function dispatch(msg: StreamMessage) {
  for (const listener of listeners) listener.onMessage(msg);
}

function refetchAll() {
  for (const listener of listeners) safeCall(listener.fetchState);
}

function haltSocket() {
  generation += 1;
  const current = source;
  source = null;
  current?.close();
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (staleTimer) {
    clearInterval(staleTimer);
    staleTimer = null;
  }
  backoff = BACKOFF_START_MS;
  mode = "stream";
}

function scheduleReconnect(gen: number) {
  if (gen !== generation || listeners.size === 0 || reconnectTimer) return;
  const delay = backoff;
  backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (gen !== generation || listeners.size === 0) return;
    connectTask = connect(gen).finally(() => {
      connectTask = null;
    });
  }, delay);
}

function bind(current: EventSource, gen: number) {
  current.addEventListener("subscribed", () => {
    backoff = BACKOFF_START_MS;
    touch();
    mode = "stream";
    publishMode();
    refetchAll();
  });
  current.addEventListener("resync", () => {
    touch();
    refetchAll();
  });
  current.addEventListener("update", (event) => {
    const raw = (event as MessageEvent).data;
    if (typeof raw !== "string") return;
    let msg: StreamMessage;
    try {
      msg = JSON.parse(raw) as StreamMessage;
    } catch {
      return;
    }
    touch();
    dispatch(msg);
  });
  current.onerror = () => {
    if (current.readyState !== EventSource.CLOSED) return;
    if (source === current) source = null;
    current.close();
    scheduleReconnect(gen);
  };
}

async function connect(gen: number) {
  let ticket: string;
  try {
    const body = await request<{ ticket: string }>("/api/events/ticket", { method: "POST" });
    ticket = body.ticket;
  } catch {
    if (gen === generation && listeners.size > 0) scheduleReconnect(gen);
    return;
  }
  if (gen !== generation || listeners.size === 0) return;
  const Source = globalThis.EventSource;
  const current = new Source(eventsUrl(ticket));
  if (gen !== generation || listeners.size === 0) {
    current.close();
    return;
  }
  source = current;
  bind(current, gen);
}

function ensureStaleTimer() {
  if (staleTimer) return;
  touch();
  staleTimer = setInterval(() => {
    if (listeners.size === 0 || mode !== "stream") return;
    if (Date.now() - lastActivity < STALE_MS) return;
    touch();
    refetchAll();
  }, POLL_MS);
}

function ensure() {
  if (listeners.size === 0 || source || reconnectTimer || connectTask) return;
  ensureStaleTimer();
  const gen = generation;
  connectTask = connect(gen).finally(() => {
    connectTask = null;
  });
}

export function stopEventStream() {
  haltSocket();
}

export function openEventStream(handlers: OpenHandlers): StreamSession {
  const session: StreamSession = {
    mode,
    close: () => undefined,
  };
  const listener: Listener = { ...handlers, session };
  listeners.add(listener);
  session.close = () => {
    listeners.delete(listener);
    if (listeners.size === 0) haltSocket();
  };
  ensure();
  return session;
}

export function useEventStream(handlers: StreamHandlers): { mode: StreamMode } {
  const [streamMode, setStreamMode] = useState<StreamMode>("stream");
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    if (typeof document === "undefined") return;
    const onVisible = () => {
      if (document.visibilityState === "visible") safeCall(() => handlersRef.current.fetchState());
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, []);

  useEffect(() => {
    const session = openEventStream({
      fetchState: () => handlersRef.current.fetchState(),
      onMessage: (msg) => handlersRef.current.onMessage(msg),
      onMode: setStreamMode,
    });
    return () => session.close();
  }, []);

  return { mode: streamMode };
}

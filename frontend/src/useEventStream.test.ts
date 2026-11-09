import { afterEach, describe, expect, it, vi } from "vitest";
import { JobMessage } from "./api";
import { applySnapshot, mergeState, openEventStream, stopEventStream } from "./useEventStream";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  url: string;
  readyState = FakeEventSource.OPEN;
  closed = false;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, ((event: MessageEvent) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    const current = this.listeners.get(type) ?? [];
    current.push(listener);
    this.listeners.set(type, current);
  }

  emit(type: string, data = "{}") {
    for (const listener of this.listeners.get(type) ?? []) listener({ data } as MessageEvent);
  }

  fail() {
    this.readyState = FakeEventSource.CLOSED;
    this.onerror?.();
  }

  close() {
    this.readyState = FakeEventSource.CLOSED;
    this.closed = true;
  }
}

const job = (seq: number, status: string): JobMessage => ({
  type: "job",
  id: "job-1",
  status,
  done_segments: status === "done" ? 4 : 1,
  total_segments: 4,
  cost_cents: 2,
  error_code: null,
  seq,
});

function mockTicket(tickets: string[]) {
  let index = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      const ticket = tickets[Math.min(index, tickets.length - 1)];
      index += 1;
      return {
        ok: true,
        status: 200,
        statusText: "OK",
        text: async () => JSON.stringify({ ticket, expires_at: "2026-01-01T00:00:00Z" }),
      };
    }),
  );
}

afterEach(() => {
  stopEventStream();
  FakeEventSource.instances = [];
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("mergeState", () => {
  it("ignores an older seq", () => {
    const current = { status: "translating", seq: 20, done_segments: 2, total_segments: 4, cost_cents: 1, error_code: null };
    expect(mergeState(current, job(19, "translating"))).toBe(current);
  });

  it("lets a terminal status win over a newer non-terminal state", () => {
    const current = { status: "translating", seq: 50, done_segments: 2, total_segments: 4, cost_cents: 1, error_code: null };
    expect(mergeState(current, job(10, "done")).status).toBe("done");
  });

  it("keeps a finished or cancelled job ahead of a later non-terminal message", () => {
    const done = { status: "done", seq: 10, done_segments: 4, total_segments: 4, cost_cents: 3, error_code: null };
    expect(mergeState(done, job(40, "translating"))).toBe(done);
    const cancelled = { ...done, status: "cancelled" };
    expect(mergeState(cancelled, job(40, "translating"))).toBe(cancelled);
  });

  it("resumes a failed job when a later message leaves that status", () => {
    const current = { status: "failed", seq: 10, done_segments: 1, total_segments: 4, cost_cents: 1, error_code: "rate_limited" as string | null };
    const next = mergeState(current, job(40, "translating"));
    expect(next.status).toBe("translating");
    expect(next.error_code).toBeNull();
    const listed = applySnapshot(current, { ...current, status: "translating", error_code: null, seq: 0 }, "job");
    expect(listed.status).toBe("translating");
  });
});

describe("openEventStream", () => {
  it("opens one ticket stream and refetches on subscribed and resync", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    mockTicket(["a b"]);
    const fetchState = vi.fn();
    const session = openEventStream({ fetchState, onMessage: () => undefined });
    try {
      await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
      const source = FakeEventSource.instances[0];
      expect(source.url).toBe("/events?ticket=a%20b");
      source.emit("subscribed");
      source.emit("resync");
      expect(fetchState).toHaveBeenCalledTimes(2);
    } finally {
      session.close();
    }
  });

  it("shares one connection across listeners", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    mockTicket(["shared"]);
    const first = openEventStream({ fetchState: () => undefined, onMessage: () => undefined });
    const second = openEventStream({ fetchState: () => undefined, onMessage: () => undefined });
    try {
      await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    } finally {
      first.close();
      second.close();
    }
    expect(FakeEventSource.instances[0].closed).toBe(true);
  });

  it("gets a new ticket after the stream closes", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    mockTicket(["first", "second"]);
    const fetchState = vi.fn();
    const session = openEventStream({ fetchState, onMessage: () => undefined });
    try {
      await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
      vi.useFakeTimers();
      FakeEventSource.instances[0].fail();
      await vi.advanceTimersByTimeAsync(1000);
      expect(FakeEventSource.instances).toHaveLength(2);
      expect(FakeEventSource.instances[1].url).toBe("/events?ticket=second");
      expect(FakeEventSource.instances[0].closed).toBe(true);
    } finally {
      session.close();
    }
  });
});

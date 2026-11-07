import { afterEach, describe, expect, it, vi } from "vitest";
import { JobMessage } from "./api";
import { eventsUrl, mergeState, openEventStream } from "./useEventStream";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
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
    this.onerror?.();
  }

  close() {
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

afterEach(() => {
  FakeEventSource.instances = [];
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("eventsUrl", () => {
  it("sorts job and document ids", () => {
    expect(eventsUrl({ jobs: ["b", "a"], documents: ["d", "c"] })).toBe("/events?job=a&job=b&document=c&document=d");
  });
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

  it("keeps a terminal status ahead of a later non-terminal message", () => {
    const current = { status: "done", seq: 10, done_segments: 4, total_segments: 4, cost_cents: 3, error_code: null };
    expect(mergeState(current, job(40, "translating"))).toBe(current);
  });
});

describe("openEventStream", () => {
  it("calls fetchState on subscribed and resync", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const fetchState = vi.fn();
    const session = openEventStream({ jobs: ["b", "a"], documents: [] }, { fetchState, onMessage: () => undefined });
    try {
      const source = FakeEventSource.instances[0];
      expect(source.url).toBe("/events?job=a&job=b");
      source.emit("subscribed");
      source.emit("resync");
      expect(fetchState).toHaveBeenCalledTimes(2);
    } finally {
      session.close();
    }
  });

  it("switches to polling after three errors", () => {
    vi.useFakeTimers();
    vi.stubGlobal("EventSource", FakeEventSource);
    const fetchState = vi.fn();
    const session = openEventStream({ jobs: ["job-1"], documents: [] }, { fetchState, onMessage: () => undefined });
    try {
      const source = FakeEventSource.instances[0];
      source.fail();
      source.fail();
      expect(session.mode).toBe("stream");
      source.fail();
      expect(session.mode).toBe("polling");
      expect(source.closed).toBe(true);
      expect(fetchState).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(5000);
      expect(fetchState).toHaveBeenCalledTimes(2);
    } finally {
      session.close();
    }
  });

  it("opens no connection for an empty id set", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const session = openEventStream({ jobs: [], documents: [] }, { fetchState: () => undefined, onMessage: () => undefined });
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(session.mode).toBe("stream");
    session.close();
  });
});

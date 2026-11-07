import { afterEach, describe, expect, it, vi } from "vitest";
import { request, setUnauthorizedHandler } from "./api";
import { eventsUrl } from "./useEventStream";

afterEach(() => {
  setUnauthorizedHandler(() => undefined);
  vi.unstubAllGlobals();
});

function mockFetch(status: number, body: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: status >= 200 && status < 300,
      status,
      statusText: "",
      text: async () => body,
    })),
  );
}

describe("auth", () => {
  it("calls onUnauthorized when a request returns 401", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    mockFetch(401, JSON.stringify({ code: "unauthorized", message: "Sign in to continue.", detail: "" }));
    await expect(request("/api/jobs")).rejects.toMatchObject({ code: "unauthorized", status: 401 });
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("leaves /auth/me unauthorized handling to the caller", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    mockFetch(401, JSON.stringify({ code: "unauthorized", message: "Sign in to continue.", detail: "" }));
    await expect(request("/api/auth/me")).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("puts the encoded ticket in the event stream url", () => {
    const ticket = "user id/token+plus";
    expect(eventsUrl(ticket)).toContain(encodeURIComponent(ticket));
    expect(eventsUrl(ticket)).toBe(`/events?ticket=${encodeURIComponent(ticket)}`);
  });
});

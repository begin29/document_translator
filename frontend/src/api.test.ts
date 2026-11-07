import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, request } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

function mockFetch(status: number, body: string, statusText = "") {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: status >= 200 && status < 300,
      status,
      statusText,
      text: async () => body,
    })),
  );
}

describe("request", () => {
  it("maps an HTML 413 to file_too_large", async () => {
    mockFetch(413, "<html><body>Request Entity Too Large</body></html>", "Request Entity Too Large");
    await expect(request("/api/documents")).rejects.toBeInstanceOf(ApiError);
    mockFetch(413, "<html><body>Request Entity Too Large</body></html>", "Request Entity Too Large");
    await expect(request("/api/documents")).rejects.toMatchObject({ code: "file_too_large", status: 413 });
  });

  it("maps an empty 502 to service_unavailable", async () => {
    mockFetch(502, "", "Bad Gateway");
    await expect(request("/api/jobs")).rejects.toMatchObject({ code: "service_unavailable", status: 502 });
  });

  it("keeps the code from a JSON error", async () => {
    mockFetch(409, JSON.stringify({ code: "not_ready", message: "Still running", detail: "queued" }), "Conflict");
    await expect(request("/api/jobs")).rejects.toMatchObject({ code: "not_ready", detail: "queued", status: 409 });
  });

  it("returns parsed JSON on success", async () => {
    mockFetch(200, JSON.stringify({ id: "job-1", status: "done" }), "OK");
    await expect(request("/api/jobs/job-1")).resolves.toEqual({ id: "job-1", status: "done" });
  });
});

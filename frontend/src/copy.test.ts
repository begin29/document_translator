import { describe, expect, it } from "vitest";
import { errorMessage, progressLabel, progressRatio, sameNotice } from "./copy";

describe("copy", () => {
  it("maps known errors to specific sentences", () => {
    expect(errorMessage("corrupt_pdf")).toMatch(/damaged/);
    expect(errorMessage("ocr_no_text")).toMatch(/scanned pages/);
    expect(errorMessage("cost_cap_exceeded")).toMatch(/cost cap/);
    expect(errorMessage("rate_limited")).toMatch(/slow down/);
    expect(errorMessage("funds_exceeded")).toMatch(/spend limit/);
    expect(errorMessage("not_a_document")).toMatch(/readable document/);
    expect(errorMessage("invalid_edit")).toMatch(/not part of this translation/);
  });

  it("treats a longer restatement of the same error as one notice", () => {
    const message = errorMessage("rate_limited");
    expect(sameNotice(message, "The translation provider asked us to slow down. The job will retry.")).toBe(true);
    expect(sameNotice(message, "retry_after=1")).toBe(false);
  });

  it("describes in-flight progress", () => {
    expect(progressLabel(3, 10, "translating")).toMatch(/3\/10/);
    expect(progressLabel(10, 10, "done")).toMatch(/complete/);
  });

  it("keeps the bar short of full until the job is done", () => {
    expect(progressRatio(0, 10, "queued")).toBeLessThan(progressRatio(0, 10, "translating"));
    expect(progressRatio(3, 10, "translating")).toBeGreaterThan(progressRatio(0, 10, "translating"));
    expect(progressRatio(10, 10, "translating")).toBeLessThan(100);
    expect(progressRatio(10, 10, "reviewing")).toBeGreaterThan(progressRatio(10, 10, "translating"));
    expect(progressRatio(10, 10, "rendering")).toBeGreaterThan(progressRatio(10, 10, "reviewing"));
    expect(progressRatio(10, 10, "rendering")).toBeLessThan(100);
    expect(progressRatio(10, 10, "done")).toBe(100);
  });
});

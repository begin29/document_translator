import { describe, expect, it } from "vitest";
import { errorMessage, progressLabel, sameNotice } from "./copy";

describe("copy", () => {
  it("maps known errors to specific sentences", () => {
    expect(errorMessage("corrupt_pdf")).toMatch(/damaged/);
    expect(errorMessage("ocr_no_text")).toMatch(/scanned pages/);
    expect(errorMessage("cost_cap_exceeded")).toMatch(/cost cap/);
    expect(errorMessage("rate_limited")).toMatch(/slow down/);
    expect(errorMessage("funds_exceeded")).toMatch(/spend limit/);
    expect(errorMessage("not_a_document")).toMatch(/readable document/);
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
});

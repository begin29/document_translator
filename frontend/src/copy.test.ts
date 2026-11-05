import { describe, expect, it } from "vitest";
import { errorMessage, progressLabel } from "./copy";

describe("copy", () => {
  it("maps known errors to specific sentences", () => {
    expect(errorMessage("corrupt_pdf")).toMatch(/damaged/);
    expect(errorMessage("ocr_no_text")).toMatch(/scanned pages/);
    expect(errorMessage("cost_cap_exceeded")).toMatch(/cost cap/);
    expect(errorMessage("not_a_document")).toMatch(/readable document/);
  });

  it("describes in-flight progress", () => {
    expect(progressLabel(3, 10, "translating")).toMatch(/3\/10/);
    expect(progressLabel(10, 10, "done")).toMatch(/complete/);
  });
});

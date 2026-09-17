import { describe, expect, it } from "vitest";
import { z } from "zod";
import { measureSchema, measureStatusSchema, type MeasureStatus } from "./cockpit";

const NON_AVAILABLE_STATUSES: Exclude<MeasureStatus, "available">[] = [
  "no_data",
  "insufficient_volume",
  "immature_window",
  "learning",
  "not_controllable",
  "no_customer_source",
  "stale",
];

describe("measureSchema — never fabricates a figure (FR-009/SC-006)", () => {
  const numberMeasure = measureSchema(z.number());

  it("accepts an available measure with its value", () => {
    const parsed = numberMeasure.safeParse({ status: "available", value: 42 });
    expect(parsed.success).toBe(true);
  });

  it.each(NON_AVAILABLE_STATUSES)("accepts %s with a null value and a server reason", (status) => {
    const parsed = numberMeasure.safeParse({ status, value: null, reason: "Ventana con menos de 30 conversiones" });
    expect(parsed.success).toBe(true);
  });

  it.each(NON_AVAILABLE_STATUSES)("rejects %s carrying a number instead of null", (status) => {
    const parsed = numberMeasure.safeParse({ status, value: 0, reason: "no debería llegar con cifra" });
    expect(parsed.success).toBe(false);
  });

  it.each(NON_AVAILABLE_STATUSES)("rejects %s without a reason", (status) => {
    const parsed = numberMeasure.safeParse({ status, value: null });
    expect(parsed.success).toBe(false);
  });

  it("rejects an available measure missing its value", () => {
    const parsed = numberMeasure.safeParse({ status: "available" });
    expect(parsed.success).toBe(false);
  });

  it("rejects a status outside the contract's enum", () => {
    const parsed = numberMeasure.safeParse({ status: "guessed", value: null, reason: "inventado" });
    expect(parsed.success).toBe(false);
  });

  it("measureStatusSchema exposes exactly the eight statuses of the contract", () => {
    expect(measureStatusSchema.options).toEqual([
      "available",
      "no_data",
      "insufficient_volume",
      "immature_window",
      "learning",
      "not_controllable",
      "no_customer_source",
      "stale",
    ]);
  });
});

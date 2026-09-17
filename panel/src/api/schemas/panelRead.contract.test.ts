import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { portfolioRowSchema, signalRowSchema } from "@/api/schemas";

const wire = JSON.parse(readFileSync(resolve(process.cwd(), "../tests/contracts/panel-read-rows.json"), "utf8"));

describe("real SQL row → JSON → panel contract", () => {
  it("accepts the backend serialized paused campaign and HOLD signal without discarding data", () => {
    expect(portfolioRowSchema.parse(wire.portfolio_row)).toEqual(wire.portfolio_row);
    expect(signalRowSchema.parse(wire.signal_row)).toEqual(wire.signal_row);
  });

  it("reproduces the installed lowercase regression; malformed wire data stays rejected", () => {
    expect(portfolioRowSchema.safeParse({ ...wire.portfolio_row, status: "paused" }).success).toBe(false);
    expect(portfolioRowSchema.safeParse({ ...wire.portfolio_row, signal: { ...wire.portfolio_row.signal, kind: "hold" } }).success).toBe(false);
    expect(signalRowSchema.safeParse({ ...wire.signal_row, kind: "hold" }).success).toBe(false);
  });
});

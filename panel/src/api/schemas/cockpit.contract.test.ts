import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { cockpitDetailRefSchema, cockpitSparklineSchema, cockpitViewSchema, rowActionSchema } from "./cockpit";
import { buildCockpit } from "@/mocks/fixtures/cockpit";
import { approveResponseSchema } from "./proposals";

describe("shared Python/TypeScript wire contracts", () => {
  it("normalizes the exact Python sparkline tuple and typed change references", () => {
    const wire = JSON.parse(readFileSync(resolve(process.cwd(), "../tests/contracts/cockpit-wire.json"), "utf8"));
    expect(cockpitSparklineSchema.parse(wire.sparkline)).toEqual({ metric: "spend", points: wire.sparkline });
    for (const ref of wire.detail_refs) expect(cockpitDetailRefSchema.parse(ref)).toEqual(ref);
  });
  it("accepts a populated cockpit with the server's series and change shape", () => {
    const view = buildCockpit("biz_norte", "7d");
    const wire = {
      ...view,
      rows: view.rows.map((row) => ({ ...row, sparkline: row.sparkline.points })),
      changes_since: {
        ...view.changes_since,
        items: view.changes_since.items.map((item) => ({ ...item, detail_ref: { kind: "signal", id: "signal-1" } })),
      },
    };
    expect(cockpitViewSchema.parse(wire).rows[0]?.sparkline).toEqual(view.rows[0]?.sparkline);
  });
  it("rejects malformed series and detail objects rather than silently discarding them", () => {
    expect(cockpitSparklineSchema.safeParse([1, 2]).success).toBe(false);
    expect(cockpitSparklineSchema.safeParse(Array(14).fill("0")).success).toBe(false);
    expect(cockpitDetailRefSchema.safeParse({ kind: "unexpected", id: "s-1" }).success).toBe(false);
    expect(cockpitDetailRefSchema.safeParse({ kind: "signal" }).success).toBe(false);
  });
  it("accepts the exact serialized backend actions, preserving hash, evidence and null target", () => {
    const actions: unknown[] = JSON.parse(readFileSync(resolve(process.cwd(), "../tests/contracts/row-action.json"), "utf8"));
    for (const action of actions) expect(rowActionSchema.parse(action)).toEqual(action);
  });
  it("accepts approval scheduled by the server before an execution undo window exists", () => {
    expect(approveResponseSchema.parse({
      authorization_id: "auth-1", execution_id: "execution-1",
      execution_scheduled_at: "2026-09-11T15:00:00Z", undo_deadline: null, grace_seconds: 30,
    }).undo_deadline).toBeNull();
  });
});

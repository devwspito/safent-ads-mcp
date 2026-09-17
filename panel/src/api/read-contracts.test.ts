import { describe, expect, it } from "vitest";
import { conversionsByKindSchema, freshnessResponseSchema } from "./schemas";

describe("read contracts from a newly connected Ads account", () => {
  it("does not reject a portfolio when only lead conversions are reported", () => {
    expect(conversionsByKindSchema.parse({ lead: 0 })).toEqual({
      lead: 0, whatsapp: null, call: null, business_conversion: null,
    });
  });
  it("preserves measured zero and rejects malformed measurements", () => {
    expect(conversionsByKindSchema.parse({ call: 0 }).call).toBe(0);
    expect(conversionsByKindSchema.safeParse({ lead: "0" }).success).toBe(false);
  });
  it("aggregates the oldest account's freshness, never the newest", () => {
    const fresh = { last_ingested_at: "2026-09-14T01:00:00Z", lag_minutes: 2, is_stale: false, no_data: false };
    const stale = { last_ingested_at: "2026-09-13T01:00:00Z", lag_minutes: 1440, is_stale: true, no_data: false };
    expect(freshnessResponseSchema.parse({ items: [fresh, stale] })).toEqual(stale);
    expect(freshnessResponseSchema.parse({ items: [stale, fresh] })).toEqual(stale);
    expect(freshnessResponseSchema.parse(fresh)).toEqual(fresh);
  });
  it("keeps an empty inventory unknown instead of declaring data fresh", () => {
    expect(freshnessResponseSchema.parse({ items: [] })).toBeNull();
    expect(freshnessResponseSchema.safeParse({ items: [{ lag_minutes: 0 }] }).success).toBe(false);
  });
  // Hotfix 0.2.20 Bug B repro: a sibling account that never ingested
  // anything must never mask a real account's freshness (or its staleness).
  it("never lets a no_data sibling mask a real account's freshness", () => {
    const fresh = { last_ingested_at: "2026-09-14T16:55:00Z", lag_minutes: 5, is_stale: false, no_data: false };
    const neverIngested = { last_ingested_at: "2026-09-14T17:00:00Z", lag_minutes: 0, is_stale: false, no_data: true };
    expect(freshnessResponseSchema.parse({ items: [fresh, neverIngested] })).toEqual(fresh);
    expect(freshnessResponseSchema.parse({ items: [neverIngested, fresh] })).toEqual(fresh);
  });
  it("reports no_data only when every account lacks data", () => {
    const a = { last_ingested_at: "2026-09-14T17:00:00Z", lag_minutes: 0, is_stale: false, no_data: true };
    const b = { last_ingested_at: "2026-09-14T17:00:00Z", lag_minutes: 0, is_stale: false, no_data: true };
    expect(freshnessResponseSchema.parse({ items: [a, b] })).toEqual(a);
  });
  it("defaults no_data to false for a backend that does not send it yet", () => {
    expect(freshnessResponseSchema.parse({ last_ingested_at: "2026-09-14T01:00:00Z", lag_minutes: 2, is_stale: false })!.no_data).toBe(false);
  });
});

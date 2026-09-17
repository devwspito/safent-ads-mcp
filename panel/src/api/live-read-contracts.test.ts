/**
 * Opt-in acceptance against actual, owner-authenticated API snapshots.
 * Snapshots stay outside the repository and must never contain credentials.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { cockpitViewSchema } from "./schemas/cockpit";
import { freshnessResponseSchema, killSwitchStateSchema, portfolioResponseSchema, signalsResponseSchema } from "./schemas";

const snapshotDir = process.env.SAFENT_LIVE_CONTRACTS_DIR;
describe.skipIf(!snapshotDir)("live Ads read API contracts", () => {
  for (const [name, schema] of Object.entries({
    cockpit: cockpitViewSchema, portfolio: portfolioResponseSchema,
    freshness: freshnessResponseSchema, signals: signalsResponseSchema,
    "kill-switch": killSwitchStateSchema,
  })) {
    it(name, () => {
      const snapshot = JSON.parse(readFileSync(join(snapshotDir!, `live-${name}.json`), "utf8"));
      expect(snapshot.status).toBe(200);
      expect(schema.safeParse(snapshot.body)).toMatchObject({ success: true });
    });
  }
});

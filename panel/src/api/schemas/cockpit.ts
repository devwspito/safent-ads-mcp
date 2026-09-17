/**
 * Zod schemas for `GET /api/v1/cockpit`
 * (`contracts/cockpit-read-model.md` del cockpit, en el runtime).
 *
 * `Measure<T>` is the wrapper that makes it impossible to fabricate a figure (FR-009/SC-006):
 * a non-"available" status can never carry a value, only a server-authored `reason`. Money
 * here travels as a decimal STRING (never a float) — distinct from the legacy `moneySchema`
 * in `api/schemas.ts`, which the pre-026 endpoints still use.
 */
import { z } from "zod";
import { platformSchema, signalKindSchema } from "@/api/schemas";

export const cockpitMoneySchema = z.object({
  amount: z.string(),
  currency: z.string().length(3),
});
export type CockpitMoney = z.infer<typeof cockpitMoneySchema>;

export const measureStatusSchema = z.enum([
  "available",
  "no_data",
  "insufficient_volume",
  "immature_window",
  "learning",
  "not_controllable",
  "no_customer_source",
  "stale",
]);
export type MeasureStatus = z.infer<typeof measureStatusSchema>;

const unavailableMeasureStatusSchema = z.enum([
  "no_data",
  "insufficient_volume",
  "immature_window",
  "learning",
  "not_controllable",
  "no_customer_source",
  "stale",
]);

/** `{status:"available",value:T} | {status:<resto>,value:null,reason:string}` — nunca un híbrido. */
export function measureSchema<T extends z.ZodTypeAny>(valueSchema: T) {
  return z.discriminatedUnion("status", [
    z.object({ status: z.literal("available"), value: valueSchema }),
    z.object({ status: unavailableMeasureStatusSchema, value: z.null(), reason: z.string() }),
  ]);
}
export type Measure<T> = { status: "available"; value: T } | { status: Exclude<MeasureStatus, "available">; value: null; reason: string };

export const cockpitWindowSchema = z.enum(["today", "7d", "30d"]);
export type CockpitWindow = z.infer<typeof cockpitWindowSchema>;

export const cockpitFreshnessSchema = z.object({
  last_ingested_at: z.string(),
  lag_minutes: z.number(),
  is_stale: z.boolean(),
});

export const cockpitDegradedAccountSchema = z.object({
  platform_account_id: z.string(),
  platform: z.string(),
  status: z.string(),
  reason: z.string(),
});
export type CockpitDegradedAccount = z.infer<typeof cockpitDegradedAccountSchema>;

const costPerLeadGroupSchema = z.object({
  actual: measureSchema(cockpitMoneySchema),
  target: measureSchema(cockpitMoneySchema),
  delta_pct: measureSchema(z.number()),
});

export const portfolioHeaderSchema = z.object({
  spend: z.object({ today: cockpitMoneySchema, mtd: cockpitMoneySchema, window: cockpitMoneySchema }),
  caps: z.object({
    daily: cockpitMoneySchema.nullable(),
    monthly: cockpitMoneySchema.nullable(),
    source: z.enum(["guardrail", "broker_hard_cap"]),
  }),
  pacing: z.object({ index_pct: z.number(), projection_pct: z.number(), days_remaining: z.number().int() }),
  projected_month_end: measureSchema(cockpitMoneySchema),
  leads: z.object({ today: measureSchema(z.number()), week: measureSchema(z.number()) }),
  customers: z.object({ today: measureSchema(z.number()), week: measureSchema(z.number()) }),
  roi: measureSchema(z.number()),
  roi_basis: z.enum(["contribution", "revenue"]),
  roi_basis_reason: z.string().nullable(),
  roas: measureSchema(z.number()),
  cost_per_lead: costPerLeadGroupSchema,
  brake: z.object({ engaged: z.boolean(), mode: z.enum(["AUTONOMOUS", "ALL"]).nullable(), since: z.string().nullable() }),
  proposals: z.object({ pending: z.number().int(), deferred: z.number().int(), critical: z.number().int() }),
});
export type PortfolioHeader = z.infer<typeof portfolioHeaderSchema>;

export const rowActionTargetSchema = z.object({
  method: z.enum(["POST", "PATCH"]),
  path: z.string(),
});
export type RowActionTarget = z.infer<typeof rowActionTargetSchema>;

export const rowActionSchema = z.object({
  kind: z.enum(["approve_increase", "apply_decrease", "apply_exit", "review_proposal", "none"]),
  mode: z.enum(["inline_approval", "autonomous_applied", "proposal", "blocked"]),
  friction: z.enum(["none", "confirm"]),
  requires_evidence: z.boolean(),
  diff_hash: z.string().nullable(),
  reason: z.string().nullable(),
  proposal_id: z.string().nullable(),
  applied_change: z
    .object({
      parameter: z.string(),
      before: z.string(),
      after: z.string(),
      applied_at: z.string(),
      undo_deadline: z.string(),
    })
    .nullable(),
  blocked_reason: z.enum(["brake_engaged", "stale_data", "guardrail", "not_controllable", "immature_window"]).nullable(),
  target: rowActionTargetSchema.nullable(),
});
export type RowAction = z.infer<typeof rowActionSchema>;

export const tickerRowSignalSchema = z.object({
  signal_id: z.string(),
  kind: signalKindSchema,
  strength: z.number(),
  cause: z.string(),
  emitted_at: z.string(),
});
export type TickerRowSignal = z.infer<typeof tickerRowSignalSchema>;

// The Python TickerRow serializes its 14-day tuple as a JSON array.
// Normalize at the wire boundary; components keep the named presentation shape.
const sparklinePointsSchema = z.array(z.number().finite()).length(14);
export const cockpitSparklineSchema = z.union([
  sparklinePointsSchema.transform((points) => ({ metric: "spend" as const, points })),
  z.object({ metric: z.literal("spend"), points: sparklinePointsSchema }),
]);

export const cockpitDetailRefSchema = z.union([
  z.object({ kind: z.enum(["signal", "proposal", "execution"]), id: z.string().min(1) }),
  z.string(),
]).nullable();

export const tickerRowSchema = z.object({
  entity_ref: z.string(),
  name: z.string(),
  level: z.enum(["campaign", "ad_set"]),
  platform: platformSchema,
  platform_account_id: z.string(),
  status: z.string(),
  signal: tickerRowSignalSchema.nullable(),
  money_at_stake: cockpitMoneySchema,
  expected_contribution_delta: measureSchema(cockpitMoneySchema),
  roi: measureSchema(z.number()),
  roas: measureSchema(z.number()),
  leads: measureSchema(z.number()),
  customers: measureSchema(z.number()),
  customer_value: measureSchema(cockpitMoneySchema),
  cost_per_lead: costPerLeadGroupSchema,
  spend: cockpitMoneySchema,
  cap: cockpitMoneySchema.nullable(),
  pacing_index_pct: measureSchema(z.number()),
  sparkline: cockpitSparklineSchema,
  freshness: z.object({ lag_minutes: z.number(), is_stale: z.boolean() }),
  is_controllable: z.boolean(),
  is_degraded: z.boolean(),
  learning_state: z.object({ is_learning: z.boolean(), reason: z.string().nullable(), since: z.string().nullable() }),
  action: rowActionSchema,
});
export type TickerRow = z.infer<typeof tickerRowSchema>;

export const changeStripItemSchema = z.object({
  kind: z.enum(["signal_changed", "entity_entered", "entity_exited", "autonomous_applied", "threshold_crossed"]),
  entity_ref: z.string(),
  entity_name: z.string(),
  before: z.string().nullable(),
  after: z.string().nullable(),
  occurred_at: z.string(),
  detail_ref: cockpitDetailRefSchema,
});
export type ChangeStripItem = z.infer<typeof changeStripItemSchema>;

export const changeStripSchema = z.object({
  since: z.string(),
  is_partial: z.boolean(),
  items: z.array(changeStripItemSchema),
});
export type ChangeStrip = z.infer<typeof changeStripSchema>;

export const cockpitViewSchema = z.object({
  business_id: z.string(),
  window: cockpitWindowSchema,
  currency: z.string().length(3),
  generated_at: z.string(),
  freshness: cockpitFreshnessSchema,
  is_partial: z.boolean(),
  degraded_accounts: z.array(cockpitDegradedAccountSchema),
  header: portfolioHeaderSchema,
  rows: z.array(tickerRowSchema),
  changes_since: changeStripSchema,
});
export type CockpitView = z.infer<typeof cockpitViewSchema>;

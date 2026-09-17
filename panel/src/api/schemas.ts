/**
 * Zod schemas for the ads-api contract (contracts/rest-api.md).
 * Every response the panel consumes is parsed through one of these before it reaches a component.
 * Enums travel in English lowercase (or UPPERCASE where the contract says so) and match the
 * DB CHECK constraints (NFR-12); the panel translates to Spanish labels at render time.
 */
import { z } from "zod";

export const moneySchema = z.object({
  amount: z.number(),
  currency: z.string().length(3),
});
export type Money = z.infer<typeof moneySchema>;

export const platformSchema = z.enum(["google", "meta"]);
export type Platform = z.infer<typeof platformSchema>;

export const entityLevelSchema = z.enum(["campaign", "ad_set", "ad", "creative"]);
export type EntityLevel = z.infer<typeof entityLevelSchema>;

/** `<platform>:<level>:<external_id>` — data-model.md §Lenguaje ubicuo. */
export const entityRefSchema = z.string().min(1);
export type EntityRef = z.infer<typeof entityRefSchema>;

export const signalKindSchema = z.enum(["BUY", "HOLD", "SELL", "EXIT"]);
export type SignalKind = z.infer<typeof signalKindSchema>;

export const creativeSignalKindSchema = z.enum(["FATIGUE", "WINNER", "LOSER"]);
export type CreativeSignalKind = z.infer<typeof creativeSignalKindSchema>;

export const anomalyKindSchema = z.literal("ANOMALY");

export const entityStatusSchema = z.enum(["ACTIVE", "PAUSED", "REMOVED", "DRIFTED", "LEARNING"]);
export type EntityStatus = z.infer<typeof entityStatusSchema>;

export const learningStateSchema = z.object({
  is_learning: z.boolean(),
  reason: z.string().nullable(),
  since: z.string().nullable(),
});

export const freshnessSchema = z.object({
  last_ingested_at: z.string(),
  lag_minutes: z.number(),
  is_stale: z.boolean(),
  // Additive (hotfix 0.2.20, Bug B): an account that never ingested
  // anything is not stale data, just absent data. Old backends omit this
  // field entirely, hence the default.
  no_data: z.boolean().default(false),
});
export type Freshness = z.infer<typeof freshnessSchema>;

/** REST returns one freshness record per account. Use the oldest account
 * that actually HAS data, never the newest — and never let a sibling
 * account that never ingested anything (`no_data`) mask a real, possibly
 * stale, account (hotfix 0.2.20, Bug B). Preserve an empty inventory as
 * unknown. */
export const freshnessResponseSchema = z.union([
  freshnessSchema,
  z.object({ items: z.array(freshnessSchema) }).transform(({ items }) => {
    const withData = items.filter((item) => !item.no_data);
    const candidates = withData.length > 0 ? withData : items;
    return candidates.reduce<Freshness | null>((oldest, item) =>
      oldest === null || item.lag_minutes > oldest.lag_minutes ? item : oldest, null);
  }),
]);
export type FreshnessResponse = z.infer<typeof freshnessResponseSchema>;

export const signalSchema = z.object({
  kind: z.union([signalKindSchema, creativeSignalKindSchema, anomalyKindSchema]),
  strength: z.number().min(0).max(100),
  cause: z.string(),
});
export type Signal = z.infer<typeof signalSchema>;

export const conversionsByKindSchema = z.object({
  // The API only includes measured channels. Missing is unknown, not zero.
  lead: z.number().nullable().default(null),
  whatsapp: z.number().nullable().default(null),
  call: z.number().nullable().default(null),
  business_conversion: z.number().nullable().default(null),
});
export type ConversionsByKind = z.infer<typeof conversionsByKindSchema>;

export const portfolioRowSchema = z.object({
  entity_ref: entityRefSchema,
  name: z.string(),
  platform: platformSchema,
  // Canonical `account_ref`, same shape `/platform-accounts` returns (hotfix 0.2.20, Bug C).
  platform_account_id: z.string(),
  // Additive: internal UUID, kept for anything that still reads it.
  platform_account_uuid: z.string(),
  status: entityStatusSchema,
  currency: z.string().length(3),
  budget: moneySchema,
  spend: moneySchema,
  cost_per_lead: moneySchema.nullable(),
  signal: signalSchema.nullable(),
  money_at_stake: moneySchema,
  is_controllable: z.boolean(),
  learning_state: learningStateSchema,
  /** Más antiguo primero, en la divisa de la fila. */
  spend_14d: z.array(z.number()).length(14),
  is_degraded: z.boolean(),
});
export type PortfolioRow = z.infer<typeof portfolioRowSchema>;

export const degradedAccountSchema = z.object({
  platform_account_id: z.string(),
  platform: platformSchema,
  status: z.string(),
  reason: z.string(),
});
export type DegradedAccount = z.infer<typeof degradedAccountSchema>;

export const portfolioResponseSchema = z.object({
  window: z.string(),
  currency: z.string().length(3),
  spend: z.object({ window: moneySchema, today: moneySchema, mtd: moneySchema }),
  /** Tope nulo = sin guardarraíl: "sin tope", nunca 0. */
  caps: z.object({
    daily: moneySchema.nullable(),
    monthly: moneySchema.nullable(),
    source: z.enum(["guardrail", "broker_hard_cap"]),
  }),
  pacing: z.object({
    index_pct: z.number(),
    projection_pct: z.number(),
    days_remaining: z.number().int(),
  }),
  conversions_by_kind: conversionsByKindSchema,
  cost_per_lead: moneySchema.nullable(),
  cost_per_business_conversion: moneySchema.nullable(),
  pending_proposals: z.number().int().min(0),
  deferred_proposals: z.number().int().min(0),
  freshness: freshnessSchema,
  deviation_vs_platform_pct: z.number().nullable(),
  is_partial: z.boolean(),
  degraded_accounts: z.array(degradedAccountSchema),
  rows: z.array(portfolioRowSchema),
});
export type PortfolioResponse = z.infer<typeof portfolioResponseSchema>;

/** `pending` sin ventana de contraste abierta · `in_progress` con `days_remaining` · `not_applicable` para HOLD/ANOMALY. */
export const outcomeStatusSchema = z.enum(["pending", "in_progress", "confirmed", "not_confirmed", "not_applicable"]);
export type OutcomeStatus = z.infer<typeof outcomeStatusSchema>;

export const signalOutcomeSchema = z.object({
  status: outcomeStatusSchema,
  days_remaining: z.number().int().nullable(),
  evaluated_at: z.string().nullable(),
});
export type SignalOutcome = z.infer<typeof signalOutcomeSchema>;

export const signalActionTakenSchema = z.enum(["autonomous", "proposal", "none"]);
export type SignalActionTaken = z.infer<typeof signalActionTakenSchema>;

export const signalRowSchema = z.object({
  signal_id: z.string(),
  entity_ref: entityRefSchema,
  entity_name: z.string(),
  platform: platformSchema,
  business_id: z.string(),
  kind: z.union([signalKindSchema, creativeSignalKindSchema, anomalyKindSchema]),
  strength: z.number().min(0).max(100),
  cause: z.string(),
  data_window: z.string(),
  money_at_stake: moneySchema,
  action_taken: signalActionTakenSchema,
  proposal_id: z.string().nullable(),
  emitted_at: z.string(),
  /** Embebido siempre (extensión aceptada del contrato): nunca nulo. */
  outcome: signalOutcomeSchema,
});
export type SignalRow = z.infer<typeof signalRowSchema>;

export const signalsResponseSchema = z.object({
  items: z.array(signalRowSchema),
  next_cursor: z.string().nullable(),
  confirmed_rate_pct: z.number().nullable(),
  /** La cabecera no muestra tasa con muestra <10. */
  confirmed_rate_sample: z.number().int(),
});
export type SignalsResponse = z.infer<typeof signalsResponseSchema>;

export const entityBadgeSchema = z.enum([
  "no_controlable",
  "en_aprendizaje",
  "presupuesto_compartido",
  "obsoleto",
]);
export type EntityBadge = z.infer<typeof entityBadgeSchema>;

export const entityChildRowSchema = z.object({
  entity_ref: entityRefSchema,
  level: entityLevelSchema,
  name: z.string(),
  status: entityStatusSchema,
  spend_today: moneySchema.nullable(),
  spend_window: moneySchema.nullable(),
  conversions_by_kind: conversionsByKindSchema.nullable(),
  cost_per_lead: moneySchema.nullable(),
  cost_per_business_conversion: moneySchema.nullable(),
  signal: signalSchema.nullable(),
  freshness: freshnessSchema.nullable(),
  badges: z.array(entityBadgeSchema),
  has_children: z.boolean(),
});
export type EntityChildRow = z.infer<typeof entityChildRowSchema>;

export const entityChildrenResponseSchema = z.object({
  parent_ref: entityRefSchema,
  parent_name: z.string(),
  level: entityLevelSchema,
  items: z.array(entityChildRowSchema),
});
export type EntityChildrenResponse = z.infer<typeof entityChildrenResponseSchema>;

export const businessSchema = z.object({
  business_id: z.string(),
  name: z.string(),
});
export type Business = z.infer<typeof businessSchema>;

/** `contracts/federated-login.md` §2 — origen de la cookie de sesión, no de la identificación. */
export const sessionOriginSchema = z.enum(["federated", "password", "bridge"]);
export type SessionOrigin = z.infer<typeof sessionOriginSchema>;

/** Instante **derivado** (última identificación federada + ventana), nunca el crudo. */
export const meSessionSchema = z.object({
  origin: sessionOriginSchema,
  fresh_identification_until: z.string().nullable(),
});
export type MeSession = z.infer<typeof meSessionSchema>;

export const meResponseSchema = z.object({
  owner_id: z.string(),
  email: z.string(),
  businesses: z.array(businessSchema),
  /** Aditivo (contracts/federated-login.md §2): ausente en un backend que no lo sirva aún. */
  session: meSessionSchema.optional(),
  /** Con el login federado apagado: siempre `false` (nunca se ofrece Google sin esto). */
  federated_login_available: z.boolean().default(false),
});
export type MeResponse = z.infer<typeof meResponseSchema>;

/** `GET /auth/federated/status` (contracts/federated-login.md §1) — 404 cuando está apagado. */
export const federatedStatusResponseSchema = z.object({
  available: z.boolean(),
});
export type FederatedStatusResponse = z.infer<typeof federatedStatusResponseSchema>;

/**
 * `POST /auth/federated/start` — la URL viaja `state`/`nonce` incluidos y siempre es `https:`
 * (Google); si el servidor devolviera otra cosa, es una respuesta que no navegamos jamás.
 */
export const federatedStartResponseSchema = z.object({
  authorization_url: z.string().url().startsWith("https://"),
  expires_at: z.string(),
});
export type FederatedStartResponse = z.infer<typeof federatedStartResponseSchema>;


export const brakeScopeKindSchema = z.enum(["global", "business", "platform_account"]);
export type BrakeScopeKind = z.infer<typeof brakeScopeKindSchema>;

export const brakeModeSchema = z.enum(["AUTONOMOUS", "ALL"]);
export type BrakeMode = z.infer<typeof brakeModeSchema>;

export const killSwitchItemSchema = z.object({
  brake_id: z.string(),
  scope_kind: brakeScopeKindSchema,
  scope_id: z.string().nullable(),
  scope_label: z.string(),
  mode: brakeModeSchema,
  engaged_at: z.string(),
  engaged_by: z.string(),
  reason: z.string().nullable(),
});
export type KillSwitchItem = z.infer<typeof killSwitchItemSchema>;

/** El más restrictivo de global ∪ business ∪ platform_account, ALL sobre AUTONOMOUS. */
export const killSwitchEffectiveSchema = z.object({
  engaged: z.boolean(),
  mode: brakeModeSchema.nullable(),
  scope_kind: z.string().nullable(),
  scope_id: z.string().nullable(),
  engaged_at: z.string().nullable(),
  reason: z.string().nullable(),
});
export type KillSwitchEffective = z.infer<typeof killSwitchEffectiveSchema>;

export const killSwitchByAccountSchema = z.object({
  platform_account_id: z.string(),
  engaged: z.boolean(),
  mode: brakeModeSchema.nullable(),
  source_scope_kind: z.string().nullable(),
});
export type KillSwitchByAccount = z.infer<typeof killSwitchByAccountSchema>;

/** Los frenos son acumulables: uno activo por ámbito, no uno en total (data-model §EmergencyBrake). */
export const killSwitchStateSchema = z.object({
  items: z.array(killSwitchItemSchema),
  effective: killSwitchEffectiveSchema,
  by_account: z.array(killSwitchByAccountSchema),
});
export type KillSwitchState = z.infer<typeof killSwitchStateSchema>;

/** `{ error: { code, message, details? } }` — rest-api.md §Seguridad transversal. */
export const apiErrorSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    details: z.record(z.unknown()).optional(),
  }),
});
export type ApiError = z.infer<typeof apiErrorSchema>;

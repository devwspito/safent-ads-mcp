/** Zod schemas — `contracts/rest-api.md` §Propuestas y aprobación (US3), reconciled v2. */
import { z } from "zod";
import { entityRefSchema, moneySchema, platformSchema, signalSchema } from "@/api/schemas";

export const proposalStateSchema = z.enum([
  "pending",
  "postponed",
  "approved",
  "scheduled",
  "executing",
  "executed",
  "rejected",
  "expired",
  "invalidated",
  "failed",
]);
export type ProposalState = z.infer<typeof proposalStateSchema>;

export const proposalUrgencySchema = z.enum(["critical", "recommended", "minor"]);
export type ProposalUrgency = z.infer<typeof proposalUrgencySchema>;

export const proposalClassificationSchema = z.enum(["routine", "important", "critical"]);
export type ProposalClassification = z.infer<typeof proposalClassificationSchema>;

/** Riesgo y fricción los decide el SERVIDOR — el panel los lee, nunca los deriva (rest-api.md §Propuestas). */
export const proposalRiskLevelSchema = z.enum(["low", "medium", "high"]);
export type ProposalRiskLevel = z.infer<typeof proposalRiskLevelSchema>;

export const proposalDiffSchema = z.object({
  parametro: z.string(),
  valor_actual: z.union([z.number(), z.string()]),
  valor_propuesto: z.union([z.number(), z.string()]),
  diff_hash: z.string(),
  currency: z.string().length(3).nullable().optional(),
});
export type ProposalDiff = z.infer<typeof proposalDiffSchema>;

export const proposalItemSchema = z.object({
  /**
   * `contracts/api.md` §1: campo aditivo — `ads-api` todavía no
   * lo manda en `/proposals` (T032 sigue pendiente en el carril BE), así que es `optional()`
   * en vez de `default()`: un valor por defecto materializaría la clave en cada propuesta
   * parseada y rompería el contrato persistido byte a byte (`proposals.contract.test.ts`).
   * Ausente ⇒ es una propuesta normal — `isPackageFeedItem` sólo mira el valor `"package"`.
   */
  item_kind: z.literal("proposal").optional(),
  /**
   * `contracts/api.md` §1: presente cuando esta propuesta pertenece a un paquete de campaña ya
   * existente (p. ej. un ajuste sobre un conjunto de anuncios creado por el paquete) — el panel
   * la enlaza a `/propuestas/paquete/{package_id}`. `null`/ausente ⇒ propuesta suelta, sin paquete.
   */
  package_id: z.string().nullable().optional(),
  action_kind: z.enum(["create_campaign", "update"]),
  proposal_id: z.string(),
  /**
   * Procedencia, no autorización — `contracts/panel.md` §2.5 (A8):
   * `{kind:"person", label}` cuando un encargo con puesto vía MCP creó la propuesta, `null`
   * cuando la creó el motor de reglas o el propio dueño. Nunca participa en `diff_hash`.
   */
  proposed_by: z.object({ kind: z.literal("person"), label: z.string() }).nullable().default(null),
  entity_ref: entityRefSchema,
  entity_name: z.string(),
  platform: platformSchema,
  /**
   * Nombre llano de la cuenta publicitaria — panel-interaction-spec.md §2.2 ("campaña ·
   * plataforma · cuenta"). Todavía no está en `contracts/rest-api.md`; opcional hasta que
   * `backend-engineer` lo añada a `/proposals` (checklists/panel-contract-followups.md).
   */
  account_label: z.string().nullable().optional(),
  /**
   * Presupuesto diario vigente de la entidad — necesario para mostrar el dinero real de una
   * propuesta de pausa (design.md §3 fix (a): "− presupuesto diario actual"), que un `diff`
   * de `estado` (ACTIVE→PAUSED) no lleva. Igual que `account_label`: opcional hasta que
   * `ads-api` lo añada a `/proposals`; el fixture lo rellena, el cliente cae a
   * `estimated_impact` si falta.
   */
  current_daily_budget: moneySchema.nullable().optional(),
  diff: proposalDiffSchema,
  classification: proposalClassificationSchema,
  urgency: proposalUrgencySchema,
  risk_level: proposalRiskLevelSchema,
  requires_expansion: z.boolean(),
  requires_typed_confirmation: z.boolean(),
  estimated_impact: moneySchema,
  cause: z.string(),
  expires_at: z.string(),
  postponed_until: z.string().nullable(),
  state: proposalStateSchema,
});
export type ProposalItem = z.infer<typeof proposalItemSchema>;

/**
 * `contracts/api.md` §1 — forma de un paquete de campaña dentro de la misma bandeja. Comparte
 * la gramática de fila (`headline`≈`describeProposalChange`, `money.daily`/`monthly_equivalent`,
 * `diff_hash`≈huella) pero no tiene `diff`/`estimated_impact`/`cause`: por eso vive en un tipo
 * propio en vez de forzar campos opcionales sobre `proposalItemSchema` (evita el estado
 * imposible "propuesta con action_kind=create_package pero sin money.daily").
 */
export const packageFeedItemSchema = z.object({
  item_kind: z.literal("package"),
  package_id: z.string(),
  proposal_id: z.null(),
  action_kind: z.literal("create_package"),
  entity_name: z.string(),
  platform: platformSchema,
  account_name: z.string(),
  headline: z.string(),
  summary: z.string(),
  money: z.object({
    daily: moneySchema,
    monthly_equivalent: moneySchema,
    total_cap: moneySchema,
  }),
  why: z.string(),
  classification: proposalClassificationSchema,
  urgency: proposalUrgencySchema,
  requires_expansion: z.boolean(),
  requires_typed_confirmation: z.boolean(),
  expires_at: z.string(),
  /** Importado de `schemas/packages.ts` evitaría un ciclo (packages.ts no depende de este módulo); se repite el enum a propósito. */
  state: z.enum([
    "draft", "proposed", "approved", "publishing", "verifying",
    "published", "partially_published", "failed", "rejected", "expired", "invalidated",
  ]),
  /** Mismo nombre de campo que `proposal.diff.diff_hash` para que la fila sea una sola gramática (api.md §1). */
  diff_hash: z.string(),
});
export type PackageFeedItem = z.infer<typeof packageFeedItemSchema>;

export const feedItemSchema = z.union([packageFeedItemSchema, proposalItemSchema]);
export type FeedItem = z.infer<typeof feedItemSchema>;

export function isPackageFeedItem(item: FeedItem): item is PackageFeedItem {
  return item.item_kind === "package";
}

export const proposalGroupKindSchema = z.enum(["cause", "calendar_event"]);
export type ProposalGroupKind = z.infer<typeof proposalGroupKindSchema>;

export const proposalGroupSchema = z.object({
  group_kind: proposalGroupKindSchema,
  /** Lente urgencia: clave de causa. Lente calendar_event: `evt:<id>` o `evt:none` (siempre el último). */
  cause_key: z.string(),
  cause: z.string(),
  count: z.number().int(),
  total_impact: moneySchema,
  batch_eligible: z.boolean(),
  closes_at: z.string().nullable(),
  proposals: z.array(feedItemSchema),
});
export type ProposalGroup = z.infer<typeof proposalGroupSchema>;

export const proposalsResponseSchema = z.object({
  lens: z.enum(["urgency", "calendar_event"]),
  pending_count: z.number().int().min(0),
  deferred_count: z.number().int().min(0),
  total_impact: moneySchema.nullable(),
  next_expiring_at: z.string().nullable(),
  attention_budget: z.object({ used: z.number().int(), limit: z.number().int() }).nullable(),
  next_cursor: z.string().nullable(),
  groups: z.array(proposalGroupSchema),
});
export type ProposalsResponse = z.infer<typeof proposalsResponseSchema>;

export const guardrailVerdictSchema = z.object({
  guardrail_id: z.string(),
  name: z.string(),
  current: z.number(),
  limit: z.number(),
  unit: z.enum(["money", "percent", "count"]),
  ok: z.boolean(),
});

export const entityHistoryEntrySchema = z.object({
  at: z.string(),
  actor_kind: z.enum(["owner", "rule_engine", "agent", "system"]),
  actor_label: z.string(),
  summary: z.string(),
  proposal_id: z.string().nullable(),
});

export const evidenceMetricSchema = z.object({
  metric: z.string(),
  unit: z.enum(["money", "percent", "count", "ratio"]).nullable(),
  actual: z.number().optional(),
  target: z.number().nullable(),
  window: z.string(),
  data_age_minutes: z.number().nullable(),
  series: z.array(z.object({ date: z.string(), value: z.number() })),
});
export type EvidenceMetric = z.infer<typeof evidenceMetricSchema>;

export const proposalDetailSchema = proposalItemSchema.extend({
  creation_plan: z.record(z.unknown()).nullable(),
  creation_plan_error: z.string().nullable(),
  execution_id: z.string().nullable(),
  cause_key: z.string(),
  signal_id: z.string().nullable(),
  signal: signalSchema.nullable(),
  rule_id: z.string().nullable(),
  rule_code: z.string().nullable(),
  rule_hit_rate_pct: z.number().nullable(),
  rule_hit_rate_sample: z.number().int(),
  data_window: z.string(),
  data_age_minutes: z.number().nullable(),
  estimated_impact_range: z.object({ low: moneySchema, high: moneySchema }).nullable(),
  evidence: z.array(evidenceMetricSchema),
  guardrail_verdicts: z.array(guardrailVerdictSchema),
  entity_history: z.array(entityHistoryEntrySchema),
  owner_context: z.string().nullable(),
  /** La construye el servidor desde entity_ref — jamás una URL de cliente (C-11). */
  platform_url: z.string().nullable(),
});
export type ProposalDetail = z.infer<typeof proposalDetailSchema>;

export const approveResponseSchema = z.object({
  authorization_id: z.string(),
  execution_id: z.string(),
  execution_scheduled_at: z.string(),
  undo_deadline: z.string().nullable(),
  grace_seconds: z.number().int(),
});
export type ApproveResponse = z.infer<typeof approveResponseSchema>;

export const batchApproveResultSchema = z.object({
  proposal_id: z.string(),
  ok: z.boolean(),
  execution_id: z.string().optional(),
  execution_scheduled_at: z.string().optional(),
  error_code: z.string().optional(),
  message: z.string().optional(),
});

/** El lote nunca es atómico: siempre 207, cada resultado con su motivo. */
export const batchApproveResponseSchema = z.object({
  approved_count: z.number().int(),
  failed_count: z.number().int(),
  grace_seconds: z.number().int(),
  execution_ids: z.array(z.string()),
  results: z.array(batchApproveResultSchema),
});
export type BatchApproveResponse = z.infer<typeof batchApproveResponseSchema>;

export const patchProposalResponseSchema = z.object({
  diff_hash: z.string(),
});

/** `reject`/`postpone`/`owner-context` → `200` sin cuerpo tipado en el contrato. */
export const acknowledgedSchema = z.object({}).passthrough();

/** `contracts/rest-api.md` §Reglas y guardarraíles (US2), reconciled v2. */
import { z } from "zod";
import { platformSchema } from "@/api/schemas";

export const autonomyLevelSchema = z.enum(["NOTIFY", "AUTO", "APPROVAL"]);
export type AutonomyLevel = z.infer<typeof autonomyLevelSchema>;

export const ruleScopeSchema = z.enum(["business", "platform_account", "campaign"]);

export const ruleSchema = z.object({
  rule_id: z.string(),
  code: z.string(),
  name: z.string(),
  scope: ruleScopeSchema,
  platform: platformSchema.nullable(),
  condition_label: z.string(),
  window: z.string(),
  action_label: z.string(),
  magnitude_pct: z.number(),
  autonomy_level: autonomyLevelSchema,
  cooldown_hours: z.number(),
  is_enabled: z.boolean(),
  firings_30d: z.number().int(),
  hit_rate_pct: z.number().nullable(),
  increases_spend: z.boolean(),
});
export type Rule = z.infer<typeof ruleSchema>;

export const rulesResponseSchema = z.object({
  items: z.array(ruleSchema),
});

export const ruleUpdateSchema = z.object({
  is_enabled: z.boolean().optional(),
  autonomy_level: autonomyLevelSchema.optional(),
  magnitude_pct: z.number().optional(),
});

export const simulateResponseSchema = z.object({
  would_fire: z.boolean(),
  reason: z.string(),
  projected_diff: z
    .object({ parametro: z.string(), valor_actual: z.number(), valor_propuesto: z.number() })
    .nullable(),
});

export const guardrailScopeSchema = z.enum(["business", "platform_account", "campaign"]);

export const guardrailSchema = z.object({
  guardrail_id: z.string(),
  scope: guardrailScopeSchema,
  scope_label: z.string(),
  daily_cap: z.number(),
  monthly_cap: z.number(),
  budget_floor: z.number(),
  budget_ceiling: z.number(),
  max_step_pct: z.number(),
  max_changes_per_entity_per_day: z.number().int(),
  min_viable_spend: z.number(),
  currency: z.string().length(3),
});
export type Guardrail = z.infer<typeof guardrailSchema>;

export const guardrailsResponseSchema = z.object({
  items: z.array(guardrailSchema),
});

export const guardrailUpdateSchema = z.object({
  daily_cap: z.number().positive(),
  monthly_cap: z.number().positive(),
  budget_floor: z.number().min(0),
  budget_ceiling: z.number().positive(),
  max_step_pct: z.number().min(1).max(100),
  max_changes_per_entity_per_day: z.number().int().min(1),
  min_viable_spend: z.number().min(0),
});
export type GuardrailUpdate = z.infer<typeof guardrailUpdateSchema>;

/**
 * T120: cuentas reales no pasan a autonomía sin las preguntas 2, 3 y 8 de spec.md
 * confirmadas POR CUENTA — rest-api.md §Reglas y guardarraíles.
 */
export const autonomyGateQuestionKeySchema = z.enum(["q2_autonomous_decrease", "q3_monthly_cap", "q8_browser_path"]);
export type AutonomyGateQuestionKey = z.infer<typeof autonomyGateQuestionKeySchema>;

export const autonomyGateMissingSchema = z.object({
  key: autonomyGateQuestionKeySchema,
  label: z.string(),
  recommended_default: z.string().nullable(),
});
export type AutonomyGateMissing = z.infer<typeof autonomyGateMissingSchema>;

export const autonomyGateConfirmedSchema = z.object({
  key: autonomyGateQuestionKeySchema,
  value: z.string(),
  confirmed_at: z.string(),
  confirmed_by: z.string(),
});

export const autonomyGateAccountSchema = z.object({
  platform_account_id: z.string(),
  label: z.string(),
  ready: z.boolean(),
  missing: z.array(autonomyGateMissingSchema),
  confirmed: z.array(autonomyGateConfirmedSchema),
});
export type AutonomyGateAccount = z.infer<typeof autonomyGateAccountSchema>;

export const autonomyGateSchema = z.object({
  ready: z.boolean(),
  accounts: z.array(autonomyGateAccountSchema),
});
export type AutonomyGate = z.infer<typeof autonomyGateSchema>;

export const autonomyGateConfirmationInputSchema = z.object({
  platform_account_id: z.string(),
  key: autonomyGateQuestionKeySchema,
  value: z.string(),
  comment: z.string().optional(),
});
export type AutonomyGateConfirmationInput = z.infer<typeof autonomyGateConfirmationInputSchema>;

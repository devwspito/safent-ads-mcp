import { z } from "zod";

export const campaignObjectives = ["OUTCOME_AWARENESS", "OUTCOME_ENGAGEMENT", "OUTCOME_LEADS", "OUTCOME_SALES", "OUTCOME_TRAFFIC", "OUTCOME_APP_PROMOTION"] as const;
export type CampaignObjective = (typeof campaignObjectives)[number];
export const campaignCategories = ["CREDIT", "EMPLOYMENT", "HOUSING", "ISSUES_ELECTIONS_POLITICS", "FINANCIAL_PRODUCTS_SERVICES"] as const;
export const campaignNetworks = ["target_google_search", "target_search_network", "target_content_network", "target_partner_search_network"] as const;
export const politicalChoices = ["CONTAINS_EU_POLITICAL_ADVERTISING", "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"] as const;
/** `google_channel_spec.GoogleAdvertisingChannelType` — una fila por canal (data-model.md §GoogleChannelSpec). */
export const googleAdvertisingChannelTypes = ["SEARCH", "DISPLAY", "DEMAND_GEN", "PERFORMANCE_MAX"] as const;
export type GoogleAdvertisingChannelType = (typeof googleAdvertisingChannelTypes)[number];
/** `google_channel_spec.GoogleBiddingStrategy` — las cuatro pujas posibles, cada una con su forma de objeto (`kind` + campos propios). */
export const googleBiddingStrategyKinds = ["MANUAL_CPC", "MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"] as const;
export type GoogleBiddingStrategyKind = (typeof googleBiddingStrategyKinds)[number];
const unique = (items: string[]) => new Set(items).size === items.length;
const common = {
  schema_version: z.literal(1),
  name: z.string().min(1).max(128).refine(value => value.trim().length > 0),
  status: z.literal("PAUSED"),
  daily_budget: z.object({
    // Exact decimal text only. Never coerce through Number/parseFloat.
    amount: z.string().regex(/^[0-9]{1,12}(?:\.[0-9]{1,2})?$/).refine(value => /[1-9]/.test(value)),
    currency: z.literal("EUR"),
  }).strict(),
};

/** `campaign_creation_args.CreationBudgetArgs` reused verbatim for a bidding sub-field (no non-zero refine there — only the top-level `daily_budget` carries that panel-side guard). */
const googleBiddingBudgetSchema = z.object({
  amount: z.string().regex(/^[0-9]{1,12}(?:\.[0-9]{1,2})?$/),
  currency: z.literal("EUR"),
}).strict();

/** `google_search_args.GoogleGeographicTargetingArgs` — mismos límites y patrón, sin resolver nombres de zona: se enseñan los identificadores tal cual llegan. */
const googleGeographicTargetingSchema = z.object({
  geo_target_constants: z.array(z.string().regex(/^geoTargetConstants\/[1-9][0-9]{0,19}$/)).min(1).max(25),
  positive_geo_target_type: z.literal("PRESENCE"),
}).strict();

/** `campaign_creation_args.ConversionGoalArgs` / `_ConversionGoalsArgs`. */
const googleConversionGoalsSchema = z.array(
  z.object({ resource_name: z.string().regex(/^customers\/[0-9]{1,20}\/conversionActions\/[0-9]{1,20}$/) }).strict(),
).min(1).max(10);

const googleManualCpcBiddingSchema = z.object({ kind: z.literal("MANUAL_CPC") }).strict();
const googleMaximizeClicksBiddingSchema = z.object({ kind: z.literal("MAXIMIZE_CLICKS"), cpc_bid_ceiling: googleBiddingBudgetSchema.optional() }).strict();
const googleMaximizeConversionsBiddingSchema = z.object({ kind: z.literal("MAXIMIZE_CONVERSIONS"), target_cpa: googleBiddingBudgetSchema.optional() }).strict();
const googleMaximizeConversionValueBiddingSchema = z.object({
  kind: z.literal("MAXIMIZE_CONVERSION_VALUE"),
  // A ratio, never `Money` (INV-17): euros of value per euro spent.
  target_roas: z.string().regex(/^[0-9]{1,2}(?:\.[0-9]{1,4})?$/).optional(),
}).strict();

/** `campaign_creation_args.GoogleBiddingArgs` — DISPLAY admite las cuatro pujas. */
const googleBiddingSchema = z.discriminatedUnion("kind", [
  googleManualCpcBiddingSchema, googleMaximizeClicksBiddingSchema, googleMaximizeConversionsBiddingSchema, googleMaximizeConversionValueBiddingSchema,
]);
export type GoogleBiddingStrategyPlan = z.infer<typeof googleBiddingSchema>;
/** `campaign_creation_args.GoogleConversionBiddingArgs` — DEMAND_GEN y PERFORMANCE_MAX sólo pujan por conversión. */
const googleConversionBiddingSchema = z.discriminatedUnion("kind", [googleMaximizeConversionsBiddingSchema, googleMaximizeConversionValueBiddingSchema]);

const googleNetworkSettingsSchema = z.object({
  target_google_search: z.boolean(), target_search_network: z.boolean(), target_content_network: z.boolean(), target_partner_search_network: z.boolean(),
}).strict();

/** `GoogleSearchNativeArgs` — idéntico a hoy (bidding_strategy sigue siendo la cadena `MANUAL_CPC`), más `geographic_targeting`/`conversion_goals` opcionales que el servidor ya manda (spec 005). */
const googleSearchNativeSchema = z.object({
  advertising_channel_type: z.literal("SEARCH"),
  bidding_strategy: z.literal("MANUAL_CPC"),
  contains_eu_political_advertising: z.enum(politicalChoices),
  network_settings: googleNetworkSettingsSchema,
  geographic_targeting: googleGeographicTargetingSchema.optional(),
  conversion_goals: googleConversionGoalsSchema.optional(),
}).strict();

/** `GoogleDisplayNativeArgs` — sin `network_settings`, puja como objeto etiquetado. */
const googleDisplayNativeSchema = z.object({
  advertising_channel_type: z.literal("DISPLAY"),
  bidding_strategy: googleBiddingSchema,
  contains_eu_political_advertising: z.enum(politicalChoices),
  geographic_targeting: googleGeographicTargetingSchema.optional(),
  conversion_goals: googleConversionGoalsSchema.optional(),
}).strict();

/** `GoogleDemandGenNativeArgs` — sólo pujas de conversión, `conversion_goals` obligatorio. */
const googleDemandGenNativeSchema = z.object({
  advertising_channel_type: z.literal("DEMAND_GEN"),
  bidding_strategy: googleConversionBiddingSchema,
  contains_eu_political_advertising: z.enum(politicalChoices),
  geographic_targeting: googleGeographicTargetingSchema.optional(),
  conversion_goals: googleConversionGoalsSchema,
}).strict();

/** `GooglePerformanceMaxNativeArgs` — misma forma que Demand Gen. */
const googlePerformanceMaxNativeSchema = z.object({
  advertising_channel_type: z.literal("PERFORMANCE_MAX"),
  bidding_strategy: googleConversionBiddingSchema,
  contains_eu_political_advertising: z.enum(politicalChoices),
  geographic_targeting: googleGeographicTargetingSchema.optional(),
  conversion_goals: googleConversionGoalsSchema,
}).strict();

/** `campaign_creation_args.GoogleCampaignNativeArgs` — unión discriminada por canal, una fila por `GoogleChannelSpec`. */
const googleCampaignNativeSchema = z.discriminatedUnion("advertising_channel_type", [
  googleSearchNativeSchema, googleDisplayNativeSchema, googleDemandGenNativeSchema, googlePerformanceMaxNativeSchema,
]);
export type GoogleCampaignNative = z.infer<typeof googleCampaignNativeSchema>;

export const campaignCreationSchema = z.discriminatedUnion("platform", [
  z.object({ ...common, platform: z.literal("google"), native: googleCampaignNativeSchema }).strict(),
  z.object({ ...common, platform: z.literal("meta"), native: z.object({
    objective: z.enum(campaignObjectives), buying_type: z.literal("AUCTION"), bid_strategy: z.literal("LOWEST_COST_WITHOUT_CAP"),
    special_ad_categories: z.array(z.enum(campaignCategories)).refine(unique),
    special_ad_category_country: z.array(z.string().regex(/^[A-Z]{2}$/)).refine(unique),
  }).strict() }).strict(),
]).superRefine((plan, context) => {
  if (plan.platform === "meta" && plan.native.special_ad_categories.length && !plan.native.special_ad_category_country.length) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: "Selecciona los países de las categorías especiales." });
  }
});
export type CampaignCreationPlan = z.infer<typeof campaignCreationSchema>;

export function campaignPlanReady(detail: { platform: string; creation_plan?: Record<string, unknown> | null; creation_plan_error?: string | null }): boolean {
  const parsed = campaignCreationSchema.safeParse(detail.creation_plan);
  return detail.creation_plan_error === null && parsed.success && parsed.data.platform === detail.platform;
}

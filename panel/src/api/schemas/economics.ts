/** `contracts/rest-api.md` §Economía unitaria — `GET /offerings`, `PUT /offerings/{id}/economics`,
 * `POST /conversions/{import,webhook-token}` (T131/T132/T220). */
import { z } from "zod";

export const paymentPlanSchema = z.enum(["none", "instalments"]);
export type PaymentPlan = z.infer<typeof paymentPlanSchema>;

export const offeringEconomicsSchema = z.object({
  vat_rate_pct: z.number(),
  delivery_cost_minor: z.number(),
  sales_cost_minor: z.number(),
  refund_rate_pct: z.number().nullable(),
  payment_plan: paymentPlanSchema,
  currency: z.string(),
  updated_at: z.string(),
});
export type OfferingEconomics = z.infer<typeof offeringEconomicsSchema>;

export const offeringSchema = z.object({
  offering_id: z.string(),
  code: z.string(),
  title: z.string(),
  is_active: z.boolean(),
  list_price: z.object({ amount: z.number(), currency: z.string() }).nullable(),
  economics: offeringEconomicsSchema.nullable(),
});
export type Offering = z.infer<typeof offeringSchema>;

export const offeringsResponseSchema = z.object({ items: z.array(offeringSchema) });

/** Cuerpo de `PUT /offerings/{id}/economics` (validación: '0 ≤ vat ≤ 100, money ≥ 0, refund
 * 0–100' — la del cliente espeja la del servidor, que sigue siendo quien decide de verdad). */
export const offeringEconomicsInputSchema = z.object({
  vat_rate_pct: z.number().min(0).max(100),
  delivery_cost_minor: z.number().int().min(0),
  sales_cost_minor: z.number().int().min(0),
  refund_rate_pct: z.number().min(0).max(100).nullable(),
  payment_plan: paymentPlanSchema,
  currency: z.string().length(3),
});
export type OfferingEconomicsInput = z.infer<typeof offeringEconomicsInputSchema>;

export const rejectedConversionRowSchema = z.object({ line: z.number(), reason: z.string() });

export const conversionsImportResultSchema = z.object({
  imported: z.number(),
  duplicates: z.number(),
  rejected: z.array(rejectedConversionRowSchema),
});
export type ConversionsImportResult = z.infer<typeof conversionsImportResultSchema>;

export const webhookTokenResponseSchema = z.object({ token: z.string() });

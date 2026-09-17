/** `contracts/rest-api.md` §Creatividades (US4), reconciled v2. */
import { z } from "zod";
import { entityRefSchema, moneySchema } from "@/api/schemas";

export const mediaKindSchema = z.enum(["image", "video", "banner", "audio"]);
export type MediaKind = z.infer<typeof mediaKindSchema>;

export const creativeFormatSchema = z.enum(["1080x1920", "1080x1080", "1200x628", "300x250", "728x90"]);
export type CreativeFormat = z.infer<typeof creativeFormatSchema>;

export const creativeSignalKindSchema = z.enum(["FATIGUE", "WINNER", "LOSER", "LEARNING"]);
export type CreativeGallerySignal = z.infer<typeof creativeSignalKindSchema>;

export const policyVerdictSchema = z.enum(["PASS", "FAIL", "PENDING"]);
export type PolicyVerdict = z.infer<typeof policyVerdictSchema>;

/** Estado NUESTRO (no de plataforma): `reject`/`regenerate` no tocan la plataforma. */
export const creativeReviewStateSchema = z.enum(["pending", "approved", "rejected"]);
export type CreativeReviewState = z.infer<typeof creativeReviewStateSchema>;

export const creativeAssetSchema = z.object({
  asset_id: z.string(),
  business_id: z.string(),
  /** `asset_id` no es texto para un humano; lo genera el brief. */
  label: z.string(),
  media_kind: mediaKindSchema,
  format: creativeFormatSchema,
  preview_url: z.string(),
  signal: creativeSignalKindSchema,
  policy_verdict: policyVerdictSchema,
  policy_findings: z.array(z.string()),
  review_state: creativeReviewStateSchema,
  spend: moneySchema,
  hook_rate_pct: z.number().nullable(),
  hold_rate_pct: z.number().nullable(),
  frequency: z.number().nullable(),
  days_in_rotation: z.number().int(),
  ads_running_on: z.array(entityRefSchema),
  signal_id: z.string().nullable(),
  brief_id: z.string().nullable(),
});
export type CreativeAsset = z.infer<typeof creativeAssetSchema>;

export const creativesResponseSchema = z.object({
  items: z.array(creativeAssetSchema),
});

export const creativeJobStateSchema = z.enum(["QUEUED", "RENDERING", "COMPOSING", "CHECKING", "READY", "FAILED", "FALLBACK_CLOUD"]);

export const creativeJobSchema = z.object({
  job_id: z.string(),
  state: creativeJobStateSchema,
  progress: z.number().min(0).max(100),
  assets: z.array(creativeAssetSchema),
  renderer_used: z.string().nullable(),
  cost_estimate: moneySchema.nullable(),
});
export type CreativeJob = z.infer<typeof creativeJobSchema>;

export const policyCheckResponseSchema = z.object({
  verdict: policyVerdictSchema,
  findings: z.array(z.string()),
});

export const creativeRejectResponseSchema = z.object({
  asset_id: z.string(),
  review_state: z.literal("rejected"),
});

export const creativeRegenerateResponseSchema = z.object({
  job_id: z.string(),
});

export const proposePublicationInputSchema = z.object({
  ad_set_ref: entityRefSchema,
  ad_copy: z.object({ headline: z.string(), primary_text: z.string(), cta: z.string() }),
  extra_asset_ids: z.array(z.string()).optional(),
  typed_confirmation: z.string(),
});
export type ProposePublicationInput = z.infer<typeof proposePublicationInputSchema>;

export const proposePublicationResponseSchema = z.object({
  proposal_id: z.string(),
  diff_hash: z.string(),
  expires_at: z.string(),
});

/**
 * Zod schemas — `contracts/api.md` §2 (`PackagePreview`) y §6
 * (candidatos de creatividad / cambio de imagen). El dinero viaja como cadena decimal — igual
 * que `cockpitMoneySchema` (`api/schemas/cockpit.ts`) y a diferencia del `moneySchema` legado
 * de `api/schemas.ts` — porque `§Tipos comunes` de este contrato lo declara explícitamente
 * `{ amount: string, currency: "EUR" }`.
 *
 * Tolerancia a campos nuevos del servidor: cada `z.object()` de este archivo usa el modo por
 * defecto de Zod (descarta claves desconocidas sin fallar) — nunca `.strict()`. Un campo nuevo
 * en `PackagePreview` no rompe el panel (tasks.md T050).
 */
import { z } from "zod";
import { platformSchema } from "@/api/schemas";

export const packageMoneySchema = z.object({
  amount: z.string(),
  currency: z.string().length(3),
});
export type PackageMoney = z.infer<typeof packageMoneySchema>;

export const packageStateSchema = z.enum([
  "draft",
  "proposed",
  "approved",
  "publishing",
  "verifying",
  "published",
  "partially_published",
  "failed",
  "rejected",
  "expired",
  "invalidated",
]);
export type PackageState = z.infer<typeof packageStateSchema>;

export const packageImagePolicySchema = z.enum(["ok", "revisar", "no_disponible"]);

export const packageAdImageSchema = z.object({
  preview_url: z.string(),
  width: z.number().int(),
  height: z.number().int(),
  alt: z.string(),
  asset_id: z.string(),
  policy: packageImagePolicySchema,
});
export type PackageAdImage = z.infer<typeof packageAdImageSchema>;

export const packageAdTextSchema = z.object({
  label: z.string(),
  value: z.string(),
});

export const packageAdPreviewSchema = z.object({
  local_ref: z.string(),
  name: z.string(),
  image: packageAdImageSchema.nullable(),
  texts: z.array(packageAdTextSchema),
  cta_label: z.string().nullable(),
  landing: z.object({ url: z.string(), display: z.string() }),
  actions: z.object({ can_replace_image: z.boolean(), can_regenerate: z.boolean() }),
});
export type PackageAdPreview = z.infer<typeof packageAdPreviewSchema>;

/**
 * `contracts/panel.md` §Grupo de recursos — lo único que ES el
 * anuncio en una `PERFORMANCE_MAX` (`ads: []` en su `AdSetPreview`, tasks.md T036/T037). Reutiliza
 * `packageAdImageSchema` para las tres imágenes fijas: llegan con la misma forma que las de un
 * anuncio normal (URL firmada por el servidor, nunca del modelo).
 */
export const packageAssetGroupPreviewSchema = z.object({
  business_name: z.string(),
  images: z.array(packageAdImageSchema),
  headlines: z.array(z.string()),
  long_headlines: z.array(z.string()),
  descriptions: z.array(z.string()),
  audience_signal_count: z.number().int(),
  final_url: z.string(),
});
export type PackageAssetGroupPreview = z.infer<typeof packageAssetGroupPreviewSchema>;

export const packageAdSetPreviewSchema = z.object({
  local_ref: z.string(),
  name: z.string(),
  // Por defecto "Conjunto de anuncios" en fixtures/servidores anteriores a T036 — tolerante
  // (T037: nunca `extra: forbid`), nunca obligatorio para no romper contra un servidor viejo.
  node_label: z.string().default("Conjunto de anuncios"),
  audience_plain: z.string(),
  geo_plain: z.string(),
  schedule_plain: z.string(),
  keywords_plain: z.array(z.string()).nullable(),
  keywords_note: z.string().nullable(),
  bid_plain: z.string().nullable(),
  asset_group: packageAssetGroupPreviewSchema.nullable().default(null),
  ads: z.array(packageAdPreviewSchema),
});
export type PackageAdSetPreview = z.infer<typeof packageAdSetPreviewSchema>;

export const packageNativeSummaryItemSchema = z.object({ label: z.string(), value: z.string() });
export type PackageNativeSummaryItem = z.infer<typeof packageNativeSummaryItemSchema>;

export const packageResearchInternalSchema = z.object({
  kind: z.enum(["brand", "catalog", "crm", "results"]),
  summary: z.string(),
  observed_at: z.string(),
});
export type PackageResearchInternal = z.infer<typeof packageResearchInternalSchema>;

export const packageResearchExternalSchema = z.object({
  kind: z.enum(["web", "meta_ad_library"]),
  summary: z.string(),
  url: z.string().nullable(),
  observed_at: z.string(),
});
export type PackageResearchExternal = z.infer<typeof packageResearchExternalSchema>;

export const packageWhySchema = z.object({
  owner_request: z.string(),
  summary: z.string(),
  success_criterion: z.string(),
  kill_criterion: z.string(),
  research: z
    .object({
      internal: z.array(packageResearchInternalSchema),
      external: z.array(packageResearchExternalSchema),
    })
    .nullable(),
});
export type PackageWhy = z.infer<typeof packageWhySchema>;

export const packageOnApproveSchema = z.object({
  creates: z.object({ campaigns: z.number().int(), ad_sets: z.number().int(), ads: z.number().int() }),
  activates: z.boolean(),
  sentence: z.string(),
  undo_sentence: z.string(),
  grace_seconds: z.number().int(),
});
export type PackageOnApprove = z.infer<typeof packageOnApproveSchema>;

export const publicationRunStateSchema = z.enum(["pending", "running", "completed", "halted"]);

export const publicationStatusSchema = z.object({
  state: publicationRunStateSchema,
  done_count: z.number().int(),
  total_count: z.number().int(),
  progress_sentence: z.string(),
  halted: z
    .object({
      reason_plain: z.string(),
      next_step_plain: z.string(),
      can_resume: z.boolean(),
    })
    .nullable(),
  campaign_entity_ref: z.string().nullable(),
  activated_at: z.string().nullable(),
  undo_deadline: z.string().nullable(),
});
export type PublicationStatus = z.infer<typeof publicationStatusSchema>;

export const packagePreviewSchema = z.object({
  package_id: z.string(),
  state: packageStateSchema,
  package_hash: z.string(),
  expires_at: z.string(),
  approvable: z.boolean(),
  not_approvable_reason: z.string().nullable(),

  platform: z.object({
    code: platformSchema,
    label: z.string(),
    account: z.object({ entity_ref: z.string(), name: z.string() }),
  }),

  campaign: z.object({
    name: z.string(),
    objective_label: z.string(),
    duration_label: z.string(),
    native_summary: z.array(packageNativeSummaryItemSchema),
    ad_sets: z.array(packageAdSetPreviewSchema),
  }),

  money: z.object({
    daily: packageMoneySchema,
    monthly_equivalent: packageMoneySchema,
    total_cap: packageMoneySchema,
    envelope: z.object({ headroom: packageMoneySchema.nullable(), reason: z.string().nullable(), label: z.string() }),
    cap_label: z.string(),
  }),

  why: packageWhySchema,
  on_approve: packageOnApproveSchema,
  publication: publicationStatusSchema.nullable(),
});
export type PackagePreview = z.infer<typeof packagePreviewSchema>;

/** §3 — fila de la lista, no el detalle. Sólo lo necesario para un índice de paquetes. */
export const packageListItemSchema = z.object({
  package_id: z.string(),
  state: packageStateSchema,
  platform: platformSchema,
  account_name: z.string(),
  campaign_name: z.string(),
  ads_count: z.number().int(),
  money: z.object({ daily: packageMoneySchema, total_cap: packageMoneySchema }),
  created_at: z.string(),
  expires_at: z.string(),
});
export type PackageListItem = z.infer<typeof packageListItemSchema>;

export const packagesResponseSchema = z.object({
  items: z.array(packageListItemSchema),
  next_cursor: z.string().nullable(),
});

/** §6 — candidatos para «Cambiar imagen»: activos `READY` del negocio con veredicto `PASS`. */
export const packageCreativeCandidateSchema = z.object({
  asset_id: z.string(),
  preview_url: z.string(),
  format: z.string(),
  policy: z.enum(["ok", "revisar"]),
  created_at: z.string(),
});
export type PackageCreativeCandidate = z.infer<typeof packageCreativeCandidateSchema>;

export const packageCreativeCandidatesResponseSchema = z.object({
  items: z.array(packageCreativeCandidateSchema),
});

/** §6 — respuesta del `PATCH`: huella nueva, invalida cualquier aprobación previa. */
export const patchPackageAdCreativeResponseSchema = z.object({
  package_hash: z.string(),
});

/** §4/R2.C — `approve`: crea la publicación y firma el sobre humano antes de escribir nada. */
export const approvePackageResponseSchema = z.object({
  publication_id: z.string(),
  authorization_id: z.string(),
  grace_seconds: z.number().int(),
  execution_starts_at: z.string(),
  approval_expires_at: z.string(),
  undo: z.object({ kind: z.literal("cancel_publication"), deadline: z.string() }),
});
export type ApprovePackageResponse = z.infer<typeof approvePackageResponseSchema>;

/** §4/R2.C — `resume`: reanuda por el primer paso no hecho, exige `package_hash` (Revisión 2). */
export const resumePackageResponseSchema = z.object({
  publication_id: z.string(),
  approval_expires_at: z.string(),
});
export type ResumePackageResponse = z.infer<typeof resumePackageResponseSchema>;

/** §5 — `undo`: dos desenlaces posibles, nunca borra. */
export const undoPackageResponseSchema = z.union([
  z.object({ undo_kind: z.literal("cancelled_publication"), sentence: z.string() }),
  z.object({
    undo_kind: z.literal("campaign_paused"),
    campaign_entity_ref: z.string(),
    execution_id: z.string(),
    sentence: z.string(),
  }),
]);
export type UndoPackageResponse = z.infer<typeof undoPackageResponseSchema>;

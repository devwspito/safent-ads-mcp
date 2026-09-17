/** `contracts/rest-api.md` §Marca — `/brand`, `/brand/assets`, `/brand/discover`, `/brand/draft`, `/brand/confirm`. */
import { z } from "zod";
import { platformSchema } from "@/api/schemas";

export const assetKindSchema = z.enum([
  "logo_vector",
  "logo_raster",
  "reference_photo",
  "icon",
  "font_file",
  "palette_definition",
]);
export type AssetKind = z.infer<typeof assetKindSchema>;

export const colorRoleSchema = z.enum(["primary", "secondary", "accent", "background", "text"]);
export type ColorRole = z.infer<typeof colorRoleSchema>;

/** Procedencia de un candidato de rastreo o de una subida manual (`manual_upload`). */
export const discoverySourceSchema = z.enum([
  "favicon",
  "apple_touch_icon",
  "og_image",
  "manifest_icon",
  "img_logo_hint",
  "inline_svg",
  "manual_upload",
  "css_custom_property",
  "css_most_used_color",
  "dominant_color_logo",
  "dominant_color_screenshot",
  "css_font_family",
  "web_font_link",
  "og_site_name",
  "title_tag",
  "schema_org_organization",
  "hero_headline",
  "tagline",
  "cta_text",
]);
export type DiscoverySource = z.infer<typeof discoverySourceSchema>;

export const socialNetworkSchema = z.enum(["instagram", "facebook", "linkedin", "tiktok", "x", "youtube", "other"]);
export type SocialNetwork = z.infer<typeof socialNetworkSchema>;

export const contactChannelKindSchema = z.enum(["email", "phone", "whatsapp", "contact_form"]);
export type ContactChannelKind = z.infer<typeof contactChannelKindSchema>;

/**
 * `GET /brand/assets` y el kit confirmado: activo ya en el almacén, con la clave de storage
 * opaca (`url`) y la ruta servible de verdad (`preview_url`, `GET /brand/assets/{asset_id}/preview`
 * — mismo campo que `creative.preview_url`, sin firma ni TTL: la autorización es la cookie de
 * sesión que el navegador ya manda en cualquier petición del mismo sitio, incluida la de un `<img>`).
 */
export const brandAssetSummarySchema = z.object({
  asset_id: z.string(),
  kind: assetKindSchema,
  url: z.string(),
  usage: z.string(),
  preview_url: z.string(),
});
export type BrandAssetSummary = z.infer<typeof brandAssetSummarySchema>;

/**
 * Candidato del borrador (rastreado o subido a mano): a pesar del nombre en el dominio
 * (`LogoCandidate`), cubre cualquier `AssetKind` — logos, iconos, fuentes, fotos de referencia
 * y paletas subidas como JSON (`brand/domain/discovery.py`, `UploadBrandAsset.from_bytes`).
 */
export const assetCandidateSchema = z.object({
  asset_id: z.string(),
  kind: assetKindSchema,
  storage_uri: z.string(),
  source: discoverySourceSchema,
  confidence: z.number().min(0).max(1),
  preview_url: z.string(),
});
export type AssetCandidate = z.infer<typeof assetCandidateSchema>;

/** Respuesta de `POST /brand/assets`: mismo candidato más el `sha256` de integridad. */
export const uploadedAssetCandidateSchema = assetCandidateSchema.extend({
  sha256: z.string(),
});
export type UploadedAssetCandidate = z.infer<typeof uploadedAssetCandidateSchema>;

export const colorCandidateSchema = z.object({
  hex: z.string(),
  source: discoverySourceSchema,
  confidence: z.number().min(0).max(1),
  role_hint: colorRoleSchema.nullable(),
});
export type ColorCandidate = z.infer<typeof colorCandidateSchema>;

export const typographyCandidateSchema = z.object({
  family: z.string(),
  source: discoverySourceSchema,
  confidence: z.number().min(0).max(1),
});
export type TypographyCandidate = z.infer<typeof typographyCandidateSchema>;

export const businessNameCandidateSchema = z.object({
  name: z.string(),
  source: discoverySourceSchema,
  confidence: z.number().min(0).max(1),
});
export type BusinessNameCandidate = z.infer<typeof businessNameCandidateSchema>;

export const socialLinkSchema = z.object({
  network: socialNetworkSchema,
  url: z.string(),
});
export type SocialLink = z.infer<typeof socialLinkSchema>;

/** Presencia del canal + URL de la página que lo muestra — nunca el dato en sí (sin PII). */
export const contactChannelSchema = z.object({
  kind: contactChannelKindSchema,
  page_url: z.string(),
});
export type ContactChannel = z.infer<typeof contactChannelSchema>;

/** Ya saneado en el servidor: sin emails ni teléfonos. */
export const copySampleSchema = z.object({
  source: discoverySourceSchema,
  text: z.string(),
});
export type CopySample = z.infer<typeof copySampleSchema>;

export const brandDraftSchema = z.object({
  business_id: z.string(),
  source_url: z.string().nullable(),
  discovered_at: z.string(),
  logo_candidates: z.array(assetCandidateSchema),
  color_candidates: z.array(colorCandidateSchema),
  typography_candidates: z.array(typographyCandidateSchema),
  business_name_candidates: z.array(businessNameCandidateSchema),
  social_links: z.array(socialLinkSchema),
  contact_channels: z.array(contactChannelSchema),
  copy_samples: z.array(copySampleSchema),
});
export type BrandDraft = z.infer<typeof brandDraftSchema>;

export const colorSwatchSchema = z.object({
  role: colorRoleSchema,
  hex: z.string(),
  contrast_ratio_on_white: z.number(),
  meets_wcag_aa_normal_text: z.boolean(),
});
export type ColorSwatch = z.infer<typeof colorSwatchSchema>;

export const legalDisclaimerSchema = z.object({
  text: z.string(),
  /** `null` = todas las plataformas conectadas. */
  applies_to: z.array(platformSchema).nullable(),
});
export type LegalDisclaimer = z.infer<typeof legalDisclaimerSchema>;

/** `is_floor: true` = suelo de seguridad del producto, nunca editable ni quitable por el
 * propietario; `false` = reclamo prohibido que el propio negocio añadió con `PUT /brand/claims`. */
export const forbiddenClaimEntrySchema = z.object({
  claim: z.string(),
  is_floor: z.boolean(),
});
export type ForbiddenClaimEntry = z.infer<typeof forbiddenClaimEntrySchema>;

export const platformConstraintSchema = z.object({
  platform: platformSchema,
  max_headline_chars: z.number().int().nullable(),
  requires_disclaimer: z.boolean(),
  notes: z.string(),
});
export type PlatformConstraint = z.infer<typeof platformConstraintSchema>;

export const brandKitSchema = z.object({
  brand_kit_id: z.string(),
  business_id: z.string(),
  typography: z.object({
    primary_family: z.string(),
    secondary_family: z.string().nullable(),
    licence_note: z.string(),
    weights: z.array(z.string()),
  }),
  palette: z.array(colorSwatchSchema),
  tone_of_voice: z.object({
    description: z.string(),
    adjectives: z.array(z.string()),
    avoid: z.array(z.string()),
  }),
  assets: z.array(brandAssetSummarySchema),
  claims_allowlist: z.array(z.string()),
  forbidden_claims: z.array(forbiddenClaimEntrySchema),
  legal_disclaimers: z.array(legalDisclaimerSchema),
  platform_constraints: z.array(platformConstraintSchema),
  is_complete: z.boolean(),
  is_confirmed: z.boolean(),
  updated_at: z.string(),
});
export type BrandKit = z.infer<typeof brandKitSchema>;

export const confirmBrandDraftInputSchema = z.object({
  primary_font: z.string().min(1),
  font_licence_note: z.string().min(1),
  tone_description: z.string().min(1),
  secondary_font: z.string().optional(),
  font_weights: z.array(z.string()).optional(),
  palette: z
    .array(
      z.object({
        role: colorRoleSchema,
        hex: z.string(),
        contrast_ratio_on_white: z.number(),
      }),
    )
    .optional(),
  tone_adjectives: z.array(z.string()).optional(),
  tone_avoid: z.array(z.string()).optional(),
  selected_asset_ids: z.array(z.string()).optional(),
});
export type ConfirmBrandDraftInput = z.infer<typeof confirmBrandDraftInputSchema>;

/** `PUT /brand/claims`: mismos límites que valida el servidor (2–80 caracteres tras recortar
 * espacios, 50 reclamos por lista) — el cliente los aplica antes de enviar, el servidor decide. */
const claimTextSchema = z.string().trim().min(2).max(80);

export const updateBrandClaimsLegalDisclaimerInputSchema = z.object({
  text: z.string().trim().min(1),
  applies_to: z.array(platformSchema).nullable(),
});
export type UpdateBrandClaimsLegalDisclaimerInput = z.infer<
  typeof updateBrandClaimsLegalDisclaimerInputSchema
>;

export const updateBrandClaimsInputSchema = z.object({
  claims_allowlist: z.array(claimTextSchema).max(50),
  forbidden_claims: z.array(claimTextSchema).max(50),
  legal_disclaimers: z.array(updateBrandClaimsLegalDisclaimerInputSchema).max(20),
});
export type UpdateBrandClaimsInput = z.infer<typeof updateBrandClaimsInputSchema>;

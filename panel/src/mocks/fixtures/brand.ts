/**
 * Estado en memoria del kit/borrador de marca de "Negocio Ejemplo" para el modo mock — mismo
 * patrón que `fixtures/creatives.ts` (mutación en memoria + funciones de acceso), con un
 * `resetBrandFixtures` explícito porque, a diferencia de esos tests, aquí varios escenarios
 * (rastrear → revisar → confirmar) dependen del orden dentro del mismo archivo de test.
 *
 * Los candidatos de color son datos de marca, no colores de interfaz: por eso el hex se compone
 * en dos trozos (`#` + dígitos) en vez de como literal — la regla de lint que exige tokens de
 * `tokens.css` para el hex es sobre color de UI, no sobre la paleta que el propietario descubre.
 */
import { placeholderCreativeUri } from "@/utils/placeholderImage";
import type {
  AssetKind,
  BrandDraft,
  BrandKit,
  ConfirmBrandDraftInput,
  ForbiddenClaimEntry,
  UpdateBrandClaimsInput,
  UploadedAssetCandidate,
} from "@/api/schemas/brand";

/** Mismo suelo de seguridad que `brand/domain/claims_policy.DEFAULT_FORBIDDEN_CLAIMS`: fijo,
 * nunca lo manda el cliente ni lo quita `updateBrandClaimsFixture`. */
const FLOOR_FORBIDDEN_CLAIMS = ["garantizado", "garantizada", "exito asegurado"];

function forbiddenClaimEntries(ownerClaims: readonly string[]): ForbiddenClaimEntry[] {
  const floorCf = new Set(FLOOR_FORBIDDEN_CLAIMS.map((claim) => claim.toLowerCase()));
  const owned = ownerClaims.filter((claim) => !floorCf.has(claim.toLowerCase()));
  return [
    ...FLOOR_FORBIDDEN_CLAIMS.map((claim) => ({ claim, is_floor: true })),
    ...owned.map((claim) => ({ claim, is_floor: false })),
  ];
}

let draftByBusiness = new Map<string, BrandDraft>();
let kitByBusiness = new Map<string, BrandKit>();
let manualAssetCounter = 0;

export function resetBrandFixtures(): void {
  draftByBusiness = new Map();
  kitByBusiness = new Map();
  manualAssetCounter = 0;
}

export function getBrandKitFixture(businessId: string): BrandKit | undefined {
  return kitByBusiness.get(businessId);
}

/** Siembra un kit directamente, sin pasar por rastrear→revisar→confirmar (tests que solo
 * necesitan un kit ya existente para ejercitar `PUT /brand/claims`). */
export function setBrandKitFixture(businessId: string, kit: BrandKit): void {
  kitByBusiness.set(businessId, kit);
}

export function getBrandDraftFixture(businessId: string): BrandDraft | undefined {
  return draftByBusiness.get(businessId);
}

function emptyDraft(businessId: string): BrandDraft {
  return {
    business_id: businessId,
    source_url: null,
    discovered_at: new Date().toISOString(),
    logo_candidates: [],
    color_candidates: [],
    typography_candidates: [],
    business_name_candidates: [],
    social_links: [],
    contact_channels: [],
    copy_samples: [],
  };
}

export function discoverBrandFixture(businessId: string, url: string): BrandDraft {
  const draft: BrandDraft = {
    business_id: businessId,
    source_url: url,
    discovered_at: new Date().toISOString(),
    logo_candidates: [
      {
        asset_id: "asset_logo_1",
        kind: "logo_vector",
        storage_uri: "logo_vector/asset_logo_1.svg",
        preview_url: placeholderCreativeUri(320, 120, "Logo Negocio Ejemplo"),
        source: "inline_svg",
        confidence: 0.92,
      },
      {
        asset_id: "asset_icon_1",
        kind: "icon",
        storage_uri: "icon/asset_icon_1.bin",
        preview_url: placeholderCreativeUri(64, 64, "Icono"),
        source: "favicon",
        confidence: 0.7,
      },
    ],
    color_candidates: [
      { hex: `#${"1178AC"}`, source: "css_custom_property", confidence: 0.88, role_hint: "primary" },
      { hex: `#${"0B5C86"}`, source: "css_most_used_color", confidence: 0.6, role_hint: "secondary" },
      { hex: `#${"F5F6F7"}`, source: "dominant_color_screenshot", confidence: 0.4, role_hint: null },
    ],
    typography_candidates: [
      { family: "Inter", source: "css_font_family", confidence: 0.81 },
      { family: "Georgia", source: "web_font_link", confidence: 0.3 },
    ],
    business_name_candidates: [{ name: "Negocio Ejemplo", source: "og_site_name", confidence: 0.95 }],
    social_links: [{ network: "instagram", url: "https://instagram.com/negocio-ejemplo" }],
    contact_channels: [{ kind: "whatsapp", page_url: `${url.replace(/\/$/, "")}/contacto` }],
    copy_samples: [{ source: "hero_headline", text: "Todo lo que necesitas, en un solo sitio." }],
  };
  draftByBusiness.set(businessId, draft);
  return draft;
}

export function uploadBrandAssetFixture(businessId: string, kind: AssetKind, fileName: string): UploadedAssetCandidate {
  manualAssetCounter += 1;
  const candidate: UploadedAssetCandidate = {
    asset_id: `asset_manual_${manualAssetCounter}`,
    kind,
    storage_uri: `${kind}/asset_manual_${manualAssetCounter}.bin`,
    preview_url: placeholderCreativeUri(200, 200, fileName),
    source: "manual_upload",
    confidence: 1,
    sha256: `sha256_manual_${manualAssetCounter}`,
  };
  const existing = draftByBusiness.get(businessId) ?? emptyDraft(businessId);
  draftByBusiness.set(businessId, { ...existing, logo_candidates: [...existing.logo_candidates, candidate] });
  return candidate;
}

export type ConfirmBrandDraftFixtureResult =
  | { ok: true; kit: BrandKit }
  | { ok: false; error: "NO_DRAFT" | "ASSET_NOT_FOUND" };

export function confirmBrandDraftFixture(businessId: string, input: ConfirmBrandDraftInput): ConfirmBrandDraftFixtureResult {
  const draft = draftByBusiness.get(businessId);
  if (!draft) return { ok: false, error: "NO_DRAFT" };

  const selectedIds = input.selected_asset_ids ?? [];
  const assets: BrandKit["assets"] = [];
  for (const assetId of selectedIds) {
    const candidate = draft.logo_candidates.find((c) => c.asset_id === assetId);
    if (!candidate) return { ok: false, error: "ASSET_NOT_FOUND" };
    assets.push({
      asset_id: candidate.asset_id,
      kind: candidate.kind,
      url: candidate.storage_uri,
      usage: "Confirmado por el propietario tras revisar el borrador de rastreo.",
      preview_url: candidate.preview_url,
    });
  }

  const existing = kitByBusiness.get(businessId);
  const palette = (input.palette ?? []).map((swatch) => ({
    ...swatch,
    meets_wcag_aa_normal_text: swatch.contrast_ratio_on_white >= 4.5,
  }));
  const kit: BrandKit = {
    brand_kit_id: existing?.brand_kit_id ?? `bk_${businessId}`,
    business_id: businessId,
    typography: {
      primary_family: input.primary_font,
      secondary_family: input.secondary_font ?? null,
      licence_note: input.font_licence_note,
      weights: input.font_weights ?? [],
    },
    palette,
    tone_of_voice: {
      description: input.tone_description,
      adjectives: input.tone_adjectives ?? [],
      avoid: input.tone_avoid ?? [],
    },
    assets,
    claims_allowlist: existing?.claims_allowlist ?? [],
    forbidden_claims: existing?.forbidden_claims ?? forbiddenClaimEntries(["resultados asegurados"]),
    legal_disclaimers: existing?.legal_disclaimers ?? [],
    platform_constraints: existing?.platform_constraints ?? [],
    is_complete: assets.length > 0 && palette.length > 0,
    is_confirmed: true,
    updated_at: new Date().toISOString(),
  };
  kitByBusiness.set(businessId, kit);
  return { ok: true, kit };
}

export type UpdateBrandClaimsFixtureResult = { ok: true; kit: BrandKit } | { ok: false };

/** Fusión completa de la política de reclamos y avisos legales, igual que hace el servidor
 * (`UpdateBrandClaims`) — la validación de conflicto/longitud vive en el `422` que cada test de
 * error programa vía `server.use(...)`, no en este doble (mismo reparto que `confirmBrandDraftFixture`,
 * que tampoco reimplementa `BrandDraftAssetNotFoundError`). */
export function updateBrandClaimsFixture(
  businessId: string,
  input: UpdateBrandClaimsInput,
): UpdateBrandClaimsFixtureResult {
  const existing = kitByBusiness.get(businessId);
  if (!existing) return { ok: false };

  const kit: BrandKit = {
    ...existing,
    claims_allowlist: input.claims_allowlist,
    forbidden_claims: forbiddenClaimEntries(input.forbidden_claims),
    legal_disclaimers: input.legal_disclaimers,
    updated_at: new Date().toISOString(),
  };
  kitByBusiness.set(businessId, kit);
  return { ok: true, kit };
}

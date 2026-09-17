import type { AssetKind, BrandKit, ColorRole, ContactChannelKind, DiscoverySource, SocialNetwork } from "@/api/schemas/brand";

export const ASSET_KIND_LABELS: Record<AssetKind, string> = {
  logo_vector: "Logotipo (vector)",
  logo_raster: "Logotipo (imagen)",
  reference_photo: "Foto de referencia",
  icon: "Icono",
  font_file: "Tipografía (archivo)",
  palette_definition: "Paleta (archivo)",
};

/** Solo estos tipos son imágenes que un `<img>` puede intentar mostrar. */
const IMAGE_ASSET_KINDS = new Set<AssetKind>(["logo_vector", "logo_raster", "reference_photo", "icon"]);

export function isImageAssetKind(kind: AssetKind): boolean {
  return IMAGE_ASSET_KINDS.has(kind);
}

export const COLOR_ROLE_LABELS: Record<ColorRole, string> = {
  primary: "Primario",
  secondary: "Secundario",
  accent: "Acento",
  background: "Fondo",
  text: "Texto",
};

export const DISCOVERY_SOURCE_LABELS: Record<DiscoverySource, string> = {
  favicon: "favicon",
  apple_touch_icon: "icono de la app",
  og_image: "imagen Open Graph",
  manifest_icon: "icono del manifest",
  img_logo_hint: "imagen con pista de logo",
  inline_svg: "SVG incrustado",
  manual_upload: "subida manual",
  css_custom_property: "variable CSS",
  css_most_used_color: "color más usado en el CSS",
  dominant_color_logo: "color dominante del logo",
  dominant_color_screenshot: "color dominante de la captura",
  css_font_family: "familia tipográfica en CSS",
  web_font_link: "enlace de fuente web",
  og_site_name: "nombre del sitio (Open Graph)",
  title_tag: "etiqueta de título",
  schema_org_organization: "datos estructurados (Organization)",
  hero_headline: "titular principal",
  tagline: "eslogan",
  cta_text: "texto de llamada a la acción",
};

export const SOCIAL_NETWORK_LABELS: Record<SocialNetwork, string> = {
  instagram: "Instagram",
  facebook: "Facebook",
  linkedin: "LinkedIn",
  tiktok: "TikTok",
  x: "X",
  youtube: "YouTube",
  other: "Otra red",
};

export const CONTACT_CHANNEL_LABELS: Record<ContactChannelKind, string> = {
  email: "Correo",
  phone: "Teléfono",
  whatsapp: "WhatsApp",
  contact_form: "Formulario de contacto",
};

/** Mismo marcador que `brand/domain/brand_kit.PLACEHOLDER_MARKER`: la vista previa sin confirmar
 * (`is_confirmed: false`) puede traer huecos rellenos con este texto en vez de un valor real. */
const PLACEHOLDER_MARKER = "REEMPLAZAR";

function hasPlaceholderMarker(kit: BrandKit): boolean {
  const texts = [
    kit.typography.primary_family,
    kit.typography.licence_note,
    kit.tone_of_voice.description,
    ...kit.legal_disclaimers.map((d) => d.text),
    ...kit.assets.map((a) => a.usage),
    ...kit.assets.map((a) => a.url),
  ];
  return texts.some((text) => text.toUpperCase().includes(PLACEHOLDER_MARKER));
}

export function isPlaceholderText(text: string): boolean {
  return text.toUpperCase().includes(PLACEHOLDER_MARKER);
}

/**
 * `is_complete`/`is_confirmed` llegan del servidor pero no el motivo — se deriva aquí con la
 * misma lógica que `BrandKit.is_complete()` (backend), solo para explicar el estado en la UI.
 */
export function describeIncompleteReason(kit: BrandKit): string | null {
  if (kit.is_complete) return null;
  if (!kit.is_confirmed) return "Es un borrador de rastreo: todavía no lo ha confirmado el propietario.";
  if (kit.assets.length === 0) return "Faltan activos confirmados (logotipos, iconos u otros archivos).";
  if (kit.palette.length === 0) return "Falta la paleta de colores.";
  if (hasPlaceholderMarker(kit)) return "Hay campos pendientes de revisar antes de darlo por completo.";
  return "Faltan datos por confirmar.";
}

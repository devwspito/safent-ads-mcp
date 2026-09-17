/**
 * Fixture de paquetes — `contracts/api.md` §2, §4, §5, §6. El
 * backend real (`ads-api`) todavía no existe (tasks.md §Bloque 5: T050-T054 van contra `msw`);
 * este módulo es la única fuente de verdad de datos hasta entonces.
 *
 * La publicación avanza **un paso por cada `GET /packages/{id}`** recibido una vez pasada la
 * ventana de cancelación — no por reloj real — para que la simulación sea determinista en los
 * tests y en las capturas de pantalla (mismo principio que `getRegenerateJob`, más abajo: "el
 * mock es determinista para los tests").
 */
import { hashSeed } from "./deterministicRandom";
import { placeholderCreativeUri } from "@/utils/placeholderImage";
import { getKillSwitchState } from "./killSwitch";

const LANDING_URL = "https://negocio-ejemplo.es/reservar";
const LANDING_DISPLAY = "negocio-ejemplo.es/reservar";
const HOUR = 3_600_000;
const PAUSE_GRACE_MS = 2 * HOUR;
/** AL-6 (Revisión 2): TTL propio del sobre de aprobación, no `_AUTHORIZATION_TTL`. */
const APPROVAL_TTL_MS = 30 * 60_000;
const now = () => Date.now();

let hashCounter = 0;
function nextHash(seed: string): string {
  hashCounter += 1;
  return `pkgh_${hashSeed(`${seed}:${hashCounter}`).toString(16).slice(0, 10)}`;
}
let publicationCounter = 0;
function nextPublicationId(): string {
  publicationCounter += 1;
  return `pub_${publicationCounter.toString().padStart(4, "0")}`;
}
let authorizationCounter = 0;
function nextAuthorizationId(): string {
  authorizationCounter += 1;
  return `auth_pkg_${authorizationCounter.toString().padStart(4, "0")}`;
}

export type PackageLifecycleState =
  | "draft"
  | "proposed"
  | "approved"
  | "publishing"
  | "verifying"
  | "published"
  | "partially_published"
  | "failed"
  | "rejected"
  | "expired"
  | "invalidated";

interface AdImageState {
  asset_id: string;
  label: string;
  width: number;
  height: number;
  policy: "ok" | "revisar" | "no_disponible";
}

interface AdState {
  local_ref: string;
  name: string;
  image: AdImageState | null;
  texts: Array<{ label: string; value: string }>;
  cta_label: string | null;
  can_replace_image: boolean;
  can_regenerate: boolean;
}

interface AssetGroupImageState {
  asset_id: string;
  alt: string;
  width: number;
  height: number;
}

/** Sólo `PERFORMANCE_MAX` (tasks.md T036/T037) — el grupo de recursos ES el anuncio, `ads: []`. */
interface AssetGroupState {
  business_name: string;
  images: AssetGroupImageState[];
  headlines: string[];
  long_headlines: string[];
  descriptions: string[];
  audience_signal_count: number;
  final_url: string;
}

interface AdSetState {
  local_ref: string;
  name: string;
  /** Por defecto "Conjunto de anuncios" — los fixtures Meta/Search de siempre no lo declaran. */
  node_label?: string;
  audience_plain: string;
  geo_plain: string;
  schedule_plain: string;
  keywords_plain: string[] | null;
  bid_plain: string | null;
  ads: AdState[];
  asset_group?: AssetGroupState;
}

type PublicationStepKind = "campaign" | "ad_set" | "ad" | "activate";

/** Estado de simulación de `PackagePublication` — data-model.md §`PackagePublication`. */
interface PublicationSimState {
  publication_id: string;
  authorization_id: string;
  grace_seconds: number;
  execution_starts_at: number;
  approval_expires_at: number;
  cursor: number;
  fail_at_step_index: number | null;
  campaign_entity_ref: string | null;
  activated_at: number | null;
  pause_undo_deadline: number | null;
  halt_reason_plain: string | null;
  halt_next_step_plain: string | null;
  halt_can_resume: boolean;
}

interface PackageRecord {
  package_id: string;
  state: PackageLifecycleState;
  package_hash: string;
  /** Huella que se aprobó — sostiene el `200` idempotente de ME-6 en una segunda llamada igual. */
  approved_hash: string | null;
  platform_code: "meta" | "google";
  platform_label: "Meta" | "Google";
  account_entity_ref: string;
  account_name: string;
  campaign_name: string;
  objective_label: string;
  duration_label: string;
  native_summary: Array<{ label: string; value: string }>;
  ad_sets: AdSetState[];
  money: {
    daily: string;
    monthly_equivalent: string;
    total_cap: string;
    headroom: string | null;
    envelope_reason: string | null;
    envelope_label: string;
    cap_label: string;
  };
  why: {
    owner_request: string;
    summary: string;
    success_criterion: string;
    kill_criterion: string;
    research: {
      internal: Array<{ kind: "brand" | "catalog" | "crm" | "results"; summary: string; observed_at: string }>;
      external: Array<{ kind: "web" | "meta_ad_library"; summary: string; url: string | null; observed_at: string }>;
    } | null;
  };
  classification: "routine" | "important" | "critical";
  urgency: "critical" | "recommended" | "minor";
  publication: PublicationSimState | null;
  /** Sólo autoría del fixture — nunca sale por la API. Atajo de pruebas/capturas (ver cabecera). */
  demo_grace_seconds?: number;
  demo_fail_at_step_index?: number | null;
}

function defaultPackages(): PackageRecord[] {
  return [
  {
    package_id: "pkg_meta_001",
    state: "proposed",
    package_hash: nextHash("pkg_meta_001"),
    approved_hash: null,
    platform_code: "meta",
    platform_label: "Meta",
    account_entity_ref: "meta:act_100000000000001",
    account_name: "Cuenta Principal",
    campaign_name: "Reserva de citas",
    objective_label: "Conseguir reservas",
    duration_label: "14 días",
    classification: "important",
    urgency: "recommended",
    publication: null,
    native_summary: [
      { label: "Objetivo", value: "Generación de clientes potenciales" },
      { label: "Categorías especiales", value: "Ninguna (declarado explícitamente)" },
      { label: "Compra y puja", value: "Subasta · menor coste sin límite de puja" },
    ],
    ad_sets: [
      {
        local_ref: "as#1",
        name: "Gente cerca que aún no te conoce",
        audience_plain: "Mujeres y hombres de 25 a 65 años, a 15 km de Valencia",
        geo_plain: "Valencia y alrededores",
        schedule_plain: "Todos los días",
        keywords_plain: null,
        bid_plain: null,
        ads: [
          {
            local_ref: "as#1/ad#1",
            name: "Cita fuera de horario",
            image: { asset_id: "pkgcr_meta_as1_ad1", label: "Cita fuera de horario", width: 1200, height: 628, policy: "ok" },
            texts: [
              { label: "Titular", value: "Reserva tu cita en menos de un minuto" },
              { label: "Texto", value: "¿Tu clínica de siempre no tiene hueco? Reserva online, sin llamadas ni esperas." },
              { label: "Descripción", value: "Confirmación al instante, incluso fuera de horario." },
            ],
            cta_label: "Reservar ahora",
            can_replace_image: true,
            can_regenerate: true,
          },
          {
            local_ref: "as#1/ad#2",
            name: "Primera visita",
            image: { asset_id: "pkgcr_meta_as1_ad2", label: "Primera visita", width: 1200, height: 628, policy: "ok" },
            texts: [
              { label: "Titular", value: "Primera visita esta semana" },
              { label: "Texto", value: "Pide cita hoy y te atendemos en los próximos días. Sin listas de espera." },
              { label: "Descripción", value: "Reserva online, confirmación inmediata." },
            ],
            cta_label: "Reservar ahora",
            can_replace_image: true,
            can_regenerate: true,
          },
        ],
      },
      {
        local_ref: "as#2",
        name: "Ya visitaron tu web",
        audience_plain: "Personas que visitaron tu web en los últimos 30 días",
        geo_plain: "Valencia y alrededores",
        schedule_plain: "De lunes a viernes, 9:00-20:00",
        keywords_plain: null,
        bid_plain: null,
        ads: [
          {
            local_ref: "as#2/ad#1",
            name: "Vuelve cuando quieras",
            image: { asset_id: "pkgcr_meta_as2_ad1", label: "Vuelve cuando quieras", width: 1200, height: 628, policy: "ok" },
            texts: [
              { label: "Titular", value: "Sigue teniendo hueco esta semana" },
              { label: "Texto", value: "Viste nuestros horarios hace poco. Todavía puedes reservar para esta semana." },
              { label: "Descripción", value: "Reserva en menos de un minuto." },
            ],
            cta_label: "Reservar ahora",
            can_replace_image: true,
            can_regenerate: true,
          },
          {
            local_ref: "as#2/ad#2",
            name: "Recordatorio",
            image: { asset_id: "pkgcr_meta_as2_ad2", label: "Recordatorio", width: 1200, height: 628, policy: "revisar" },
            texts: [
              { label: "Titular", value: "¿Sigues necesitando cita?" },
              { label: "Texto", value: "Aún tenemos hueco esta semana. Resérvalo antes de que se ocupe." },
              { label: "Descripción", value: "Confirmación al instante." },
            ],
            cta_label: "Reservar ahora",
            can_replace_image: true,
            can_regenerate: true,
          },
        ],
      },
    ],
    money: {
      daily: "20.00",
      monthly_equivalent: "600.00",
      total_cap: "280.00",
      headroom: "1240.00",
      envelope_reason: null,
      envelope_label: "Te queda 1.240 € de tope este mes.",
      cap_label: "Nunca gastará más de 280 € en total.",
    },
    why: {
      owner_request: "Hazme una campaña de reserva de citas veterinarias.",
      summary:
        "Tus clientes buscan cita fuera de horario y no hay campaña que los recoja. Meta funciona bien para captar gente cerca que aún no te conoce.",
      success_criterion: "Coste por reserva por debajo de 12 €.",
      kill_criterion: "Si en 7 días no hay ninguna reserva, se pausa sola.",
      research: {
        internal: [
          { kind: "brand", summary: "Tu web ya tiene un formulario de reserva listo en negocio-ejemplo.es/reservar.", observed_at: new Date(now() - 2 * HOUR).toISOString() },
          { kind: "results", summary: "Tus campañas de búsqueda actuales traen leads a 8 € de media.", observed_at: new Date(now() - 4 * HOUR).toISOString() },
        ],
        external: [
          { kind: "meta_ad_library", summary: "Tres negocios cercanos anuncian «cita en el día» en Meta esta semana.", url: "https://www.facebook.com/ads/library/?q=reserva%20de%20citas", observed_at: new Date(now() - 6 * HOUR).toISOString() },
        ],
      },
    },
  },
  {
    package_id: "pkg_google_001",
    state: "proposed",
    package_hash: nextHash("pkg_google_001"),
    approved_hash: null,
    platform_code: "google",
    platform_label: "Google",
    account_entity_ref: "google:100-000-0002",
    account_name: "Cuenta Principal",
    campaign_name: "Búsqueda Reservas",
    objective_label: "Conseguir reservas",
    duration_label: "30 días",
    classification: "important",
    urgency: "recommended",
    publication: null,
    native_summary: [
      { label: "Tipo y puja", value: "Google Search · CPC manual" },
      { label: "Publicidad política UE", value: "No contiene publicidad política UE" },
    ],
    ad_sets: [
      {
        local_ref: "as#1",
        name: "Grupo de anuncios reservas",
        audience_plain: "Personas que buscan cita o reserva en Google cerca de ti",
        geo_plain: "Valencia y alrededores",
        schedule_plain: "Todos los días",
        keywords_plain: [
          "reserva cita veterinario",
          "veterinario urgencias valencia",
          "clinica veterinaria cerca de mi",
          "cita veterinario online",
          "veterinario 24 horas valencia",
          "consulta veterinaria valencia",
          "veterinario a domicilio valencia",
          "urgencias veterinarias valencia",
          "clinica veterinaria valencia centro",
          "veterinario barato valencia",
          "cita previa veterinario",
          "veterinario fin de semana valencia",
        ],
        bid_plain: "Hasta 0,80 € por clic",
        ads: [
          {
            local_ref: "as#1/ad#1",
            name: "Anuncio 1",
            image: null,
            texts: [
              { label: "Titular", value: "Reserva cita veterinaria online" },
              { label: "Titular", value: "Atención fuera de horario" },
              { label: "Titular", value: "Confirmación al instante" },
              { label: "Descripción", value: "Pide cita en menos de un minuto, sin llamadas. Hueco esta semana." },
              { label: "Descripción", value: "Urgencias y consultas. Reserva online, confirmamos al momento." },
            ],
            cta_label: null,
            can_replace_image: false,
            can_regenerate: false,
          },
          {
            local_ref: "as#1/ad#2",
            name: "Anuncio 2",
            image: null,
            texts: [
              { label: "Titular", value: "Veterinario cerca de ti" },
              { label: "Titular", value: "Cita hoy o mañana" },
              { label: "Titular", value: "Sin listas de espera" },
              { label: "Descripción", value: "Clínica veterinaria con hueco esta semana. Reserva en un minuto." },
              { label: "Descripción", value: "Precios claros desde el primer momento. Pide tu cita online." },
            ],
            cta_label: null,
            can_replace_image: false,
            can_regenerate: false,
          },
          {
            local_ref: "as#1/ad#3",
            name: "Anuncio 3",
            image: null,
            texts: [
              { label: "Titular", value: "Urgencias veterinarias 24h" },
              { label: "Titular", value: "Reserva sin llamar" },
              { label: "Titular", value: "Te confirmamos al momento" },
              { label: "Descripción", value: "Atendemos urgencias todos los días del año. Reserva online ahora." },
              { label: "Descripción", value: "Equipo veterinario disponible fuera de horario habitual." },
            ],
            cta_label: null,
            can_replace_image: false,
            can_regenerate: false,
          },
        ],
      },
    ],
    money: {
      daily: "12.00",
      monthly_equivalent: "360.00",
      total_cap: "360.00",
      headroom: null,
      envelope_reason: "No has puesto un tope mensual en Ajustes.",
      envelope_label: "Tope mensual no configurado.",
      cap_label: "Nunca gastará más de 360 € al mes.",
    },
    why: {
      owner_request: "Hazme una campaña de reserva de citas veterinarias.",
      summary:
        "La gente que busca «veterinario» en Google ya quiere reservar ahora mismo. Es la intención más directa que puedes captar.",
      success_criterion: "Coste por reserva por debajo de 10 €.",
      kill_criterion: "Si en 7 días no hay ninguna reserva, se pausa sola.",
      research: null,
    },
  },
  {
    package_id: "pkg_google_pmax_001",
    state: "proposed",
    package_hash: nextHash("pkg_google_pmax_001"),
    approved_hash: null,
    platform_code: "google",
    platform_label: "Google",
    account_entity_ref: "google:100-000-0002",
    account_name: "Cuenta Principal",
    campaign_name: "Máximo rendimiento reservas",
    objective_label: "Conseguir reservas",
    duration_label: "30 días",
    classification: "important",
    urgency: "recommended",
    publication: null,
    // T036/T037 (tasks.md): un grupo de recursos ES el anuncio -- `ads: []` a propósito, nunca
    // una sección «Qué se va a publicar» vacía (mismo criterio que 003 ME-3).
    native_summary: [
      { label: "Canal", value: "Máximo rendimiento" },
      { label: "Tipo y puja", value: "Maximizar conversiones, objetivo 12,00 € por conversión" },
      { label: "Optimiza para", value: "Conseguir reservas" },
      {
        label: "Aviso de gasto",
        value: "Algunos días puede gastar hasta el doble del diario; el mes no pasa de 600 €.",
      },
      {
        label: "Automatización",
        value: "Sólo se anuncia lo que has aprobado: ni páginas ni textos añadidos por Google.",
      },
      { label: "Grupos de recursos", value: "1 bloque" },
      { label: "Publicidad política UE", value: "No contiene" },
    ],
    ad_sets: [
      {
        local_ref: "as#1",
        name: "Grupo de recursos reservas",
        node_label: "Grupo de recursos",
        audience_plain: "Personas con intención de reservar cerca de tu clínica",
        geo_plain: "Configurado en Google Ads",
        schedule_plain: "Todos los días",
        keywords_plain: null,
        bid_plain: null,
        ads: [],
        asset_group: {
          business_name: "Clínica X",
          images: [
            { asset_id: "pkgcr_pmax_logo", alt: "Logo", width: 1080, height: 1080 },
            { asset_id: "pkgcr_pmax_marketing", alt: "Imagen de marketing", width: 1200, height: 628 },
            { asset_id: "pkgcr_pmax_square", alt: "Imagen cuadrada", width: 1080, height: 1080 },
          ],
          headlines: ["Reserva ya", "Cita veterinaria", "Atención 24h"],
          long_headlines: ["Reserva tu cita veterinaria en minutos"],
          descriptions: ["Reserva tu cita en minutos", "Atención profesional cercana"],
          audience_signal_count: 0,
          final_url: "https://negocio-ejemplo.es/reservar",
        },
      },
    ],
    money: {
      daily: "20.00",
      monthly_equivalent: "600.00",
      total_cap: "600.00",
      headroom: "1240.00",
      envelope_reason: null,
      envelope_label: "Te queda 1.240 € de tope este mes.",
      cap_label: "Nunca gastará más de 600 € al mes.",
    },
    why: {
      owner_request: "Hazme una campaña de máximo rendimiento para reservas.",
      summary:
        "Ya medís conversiones de reserva y tenéis tres imágenes listas. Máximo rendimiento amplía el alcance a más inventario de Google sin gestionar grupos de anuncios uno a uno.",
      success_criterion: "Coste por reserva por debajo de 12 €.",
      kill_criterion: "Si en 7 días no hay ninguna reserva, se pausa sola.",
      research: null,
    },
  },
  {
    package_id: "pkg_meta_partial_demo",
    state: "proposed",
    package_hash: nextHash("pkg_meta_partial_demo"),
    approved_hash: null,
    platform_code: "meta",
    platform_label: "Meta",
    account_entity_ref: "meta:act_100000000000001",
    account_name: "Cuenta Principal",
    campaign_name: "Recordatorio de citas",
    objective_label: "Conseguir reservas",
    duration_label: "7 días",
    classification: "important",
    urgency: "recommended",
    publication: null,
    // Sólo para pruebas/capturas — §T053: falla determinista en el segundo anuncio (cursor 2 de 5).
    demo_grace_seconds: 3,
    demo_fail_at_step_index: 2,
    native_summary: [
      { label: "Objetivo", value: "Generación de clientes potenciales" },
      { label: "Categorías especiales", value: "Ninguna (declarado explícitamente)" },
      { label: "Compra y puja", value: "Subasta · menor coste sin límite de puja" },
    ],
    ad_sets: [
      {
        local_ref: "as#1",
        name: "Clientes con cita antigua",
        audience_plain: "Personas que reservaron hace más de 60 días",
        geo_plain: "Valencia y alrededores",
        schedule_plain: "Todos los días",
        keywords_plain: null,
        bid_plain: null,
        ads: [
          {
            local_ref: "as#1/ad#1",
            name: "Toca revisión",
            image: { asset_id: "pkgcr_meta_demo_ad1", label: "Toca revisión", width: 1200, height: 628, policy: "ok" },
            texts: [
              { label: "Titular", value: "¿Ya te tocaba revisión?" },
              { label: "Texto", value: "Ha pasado tiempo desde tu última visita. Reserva en un minuto." },
              { label: "Descripción", value: "Confirmación al instante." },
            ],
            cta_label: "Reservar ahora",
            can_replace_image: true,
            can_regenerate: true,
          },
          {
            local_ref: "as#1/ad#2",
            name: "Recordatorio suave",
            image: { asset_id: "pkgcr_meta_demo_ad2", label: "Recordatorio suave", width: 1200, height: 628, policy: "ok" },
            texts: [
              { label: "Titular", value: "Seguimos aquí para ti" },
              { label: "Texto", value: "Pide tu próxima cita cuando quieras, sin llamadas." },
              { label: "Descripción", value: "Reserva online en menos de un minuto." },
            ],
            cta_label: "Reservar ahora",
            can_replace_image: true,
            can_regenerate: true,
          },
        ],
      },
    ],
    money: {
      daily: "8.00",
      monthly_equivalent: "240.00",
      total_cap: "56.00",
      headroom: "1240.00",
      envelope_reason: null,
      envelope_label: "Te queda 1.240 € de tope este mes.",
      cap_label: "Nunca gastará más de 56 € en total.",
    },
    why: {
      owner_request: "Manda un recordatorio a quien no vuelve desde hace tiempo.",
      summary: "Tienes clientes que no reservan desde hace más de dos meses. Un recordatorio suave suele traerlos de vuelta.",
      success_criterion: "Coste por reserva por debajo de 9 €.",
      kill_criterion: "Si en 7 días no hay ninguna reserva, se pausa sola.",
      research: null,
    },
  },
  ];
}

let PACKAGES: PackageRecord[] = defaultPackages();

/** Restaura los paquetes de ejemplo — usar entre tests que aprueban, cambian imagen o regeneran. */
export function resetPackagesFixtures() {
  PACKAGES = defaultPackages();
  REGENERATE_JOBS.clear();
  publicationCounter = 0;
  authorizationCounter = 0;
}

function findPackage(packageId: string): PackageRecord | undefined {
  return PACKAGES.find((pkg) => pkg.package_id === packageId);
}

function findAd(pkg: PackageRecord, adLocalRef: string): { adSet: AdSetState; ad: AdState } | undefined {
  for (const adSet of pkg.ad_sets) {
    const ad = adSet.ads.find((item) => item.local_ref === adLocalRef);
    if (ad) return { adSet, ad };
  }
  return undefined;
}

function imageAssetOf(image: AdImageState): { preview_url: string; width: number; height: number; alt: string; asset_id: string; policy: AdImageState["policy"] } {
  return {
    preview_url: placeholderCreativeUri(image.width, image.height, image.label),
    width: image.width,
    height: image.height,
    alt: image.label,
    asset_id: image.asset_id,
    policy: image.policy,
  };
}

/** data-model.md §`PackagePublication`: campaña → conjuntos → anuncios → activación, en ese orden. */
function buildStepPlan(pkg: PackageRecord): Array<{ kind: PublicationStepKind; local_ref: string }> {
  const steps: Array<{ kind: PublicationStepKind; local_ref: string }> = [{ kind: "campaign", local_ref: "campaign" }];
  for (const adSet of pkg.ad_sets) steps.push({ kind: "ad_set", local_ref: adSet.local_ref });
  for (const adSet of pkg.ad_sets) for (const ad of adSet.ads) steps.push({ kind: "ad", local_ref: ad.local_ref });
  steps.push({ kind: "activate", local_ref: "campaign" });
  return steps;
}

function haltReasonFor(kind: PublicationStepKind): string {
  if (kind === "campaign") return "Meta no pudo crear la campaña: la cuenta rechazó la petición.";
  if (kind === "ad_set") return "No se pudo crear uno de los conjuntos de anuncios.";
  if (kind === "activate") return "Se creó todo, pero la plataforma no pudo activar la campaña.";
  return "No se pudo crear uno de los anuncios: la plataforma rechazó la imagen.";
}

/**
 * Avanza la publicación un paso — se llama en cada lectura (`GET /packages/{id}` y el listado
 * de la bandeja), nunca por un temporizador propio: así la simulación es la misma tanto si el
 * panel sondea cada 10 s (design.md §2.6) como si un test adelanta el reloj y vuelve a pedir.
 */
function advancePublication(pkg: PackageRecord) {
  const pub = pkg.publication;
  if (!pub) return;

  if (pkg.state === "approved") {
    if (now() < pub.execution_starts_at) return;
    pkg.state = "publishing";
  }

  if (pkg.state !== "publishing") return;

  const steps = buildStepPlan(pkg);
  if (pub.cursor >= steps.length) return;

  if (pub.fail_at_step_index !== null && pub.cursor === pub.fail_at_step_index) {
    pub.halt_reason_plain = haltReasonFor(steps[pub.cursor]!.kind);
    pub.halt_next_step_plain = "Puedes continuar desde donde se quedó, o revisarlo en Campañas.";
    pub.halt_can_resume = true;
    pkg.state = pub.cursor === 0 ? "failed" : "partially_published";
    return;
  }

  const finishedStep = steps[pub.cursor]!;
  pub.cursor += 1;
  if (finishedStep.kind === "campaign") pub.campaign_entity_ref = `${pkg.platform_code}:campaign:${pkg.package_id}`;

  if (pub.cursor >= steps.length) {
    pkg.state = "published";
    pub.activated_at = now();
    pub.pause_undo_deadline = now() + PAUSE_GRACE_MS;
  }
}

function computePublicationStatus(pkg: PackageRecord) {
  const pub = pkg.publication;
  if (!pub) return null;
  const total = buildStepPlan(pkg).length;

  if (pkg.state === "approved") {
    return {
      state: "pending" as const,
      done_count: 0,
      total_count: total,
      progress_sentence: "Todavía no se ha creado nada.",
      halted: null,
      campaign_entity_ref: null,
      activated_at: null,
      undo_deadline: new Date(pub.execution_starts_at).toISOString(),
    };
  }

  if (pkg.state === "publishing") {
    return {
      state: "running" as const,
      done_count: pub.cursor,
      total_count: total,
      progress_sentence: `Creado ${pub.cursor} de ${total}. Nada está entregando todavía.`,
      halted: null,
      campaign_entity_ref: pub.campaign_entity_ref,
      activated_at: null,
      undo_deadline: null,
    };
  }

  if (pkg.state === "published") {
    const pauseStillOpen = pub.pause_undo_deadline !== null && now() < pub.pause_undo_deadline;
    return {
      state: "completed" as const,
      done_count: total,
      total_count: total,
      progress_sentence: "Campaña publicada y activa.",
      halted: null,
      campaign_entity_ref: pub.campaign_entity_ref,
      activated_at: pub.activated_at ? new Date(pub.activated_at).toISOString() : null,
      undo_deadline: pauseStillOpen ? new Date(pub.pause_undo_deadline!).toISOString() : null,
    };
  }

  if (pkg.state === "partially_published" || pkg.state === "failed") {
    return {
      state: "halted" as const,
      done_count: pub.cursor,
      total_count: total,
      progress_sentence: `Creado ${pub.cursor} de ${total}.`,
      halted: {
        reason_plain: pub.halt_reason_plain ?? "No se pudo completar la publicación.",
        next_step_plain: pub.halt_next_step_plain ?? "Revísalo en Campañas.",
        can_resume: pkg.state === "partially_published" && pub.halt_can_resume,
      },
      campaign_entity_ref: pub.campaign_entity_ref,
      activated_at: null,
      undo_deadline: null,
    };
  }

  return null;
}

function notApprovableReasonFor(state: PackageLifecycleState): string | null {
  const reasons: Partial<Record<PackageLifecycleState, string>> = {
    draft: "Todavía no está lista para decidir.",
    approved: "Ya se aprobó; se está a punto de publicar.",
    publishing: "Publicándose ahora mismo.",
    verifying: "Esperando confirmación de la plataforma.",
    published: "Ya está publicada.",
    partially_published: "Publicación a medias: continúa o revísalo en Campañas.",
    failed: "No se pudo crear: vuelve a proponerla.",
    rejected: "Se rechazó.",
    expired: "Caducó.",
    invalidated: "El paquete cambió después de aprobarse.",
  };
  return reasons[state] ?? null;
}

/**
 * Un grupo de recursos ES el anuncio en `PERFORMANCE_MAX` (`ads: []`,
 * tasks.md T036/T037) -- la frase no puede decir «0 anuncios» (mismo bug
 * y misma frase que `panel_read._pmax_on_approve_sentence`, backend).
 */
function pmaxOnApproveSentence(pkg: PackageRecord, assetGroups: AssetGroupState[]): string {
  const groupCount = assetGroups.length;
  const imageCount = assetGroups.reduce((sum, group) => sum + group.images.length, 0);
  const noun = groupCount === 1 ? "grupo" : "grupos";
  return (
    `Se crearán 1 campaña y ${groupCount} ${noun} de recursos con ${imageCount} imágenes en ` +
    `${pkg.platform_label} · ${pkg.account_name}, y la campaña quedará activa.`
  );
}

function onApproveFor(pkg: PackageRecord) {
  const adSetsCount = pkg.ad_sets.length;
  const adsCount = pkg.ad_sets.reduce((sum, adSet) => sum + adSet.ads.length, 0);
  const assetGroups = pkg.ad_sets.flatMap((adSet) => (adSet.asset_group ? [adSet.asset_group] : []));
  const unitLabel = pkg.platform_code === "meta" ? "conjuntos" : adSetsCount === 1 ? "grupo" : "grupos";
  const sentence =
    assetGroups.length > 0
      ? pmaxOnApproveSentence(pkg, assetGroups)
      : `Se crearán 1 campaña, ${adSetsCount} ${unitLabel} y ${adsCount} anuncios en ${pkg.platform_label} · ${pkg.account_name}, y la campaña quedará activa.`;
  return {
    creates: { campaigns: 1, ad_sets: adSetsCount, ads: adsCount },
    activates: true,
    sentence,
    undo_sentence: `Tienes ${pkg.demo_grace_seconds ?? 45} segundos para cancelarlo entero. Después, «Deshacer» pausa la campaña durante 2 horas.`,
    grace_seconds: pkg.demo_grace_seconds ?? 45,
  };
}

/** `GET /packages/{id}` — §2. */
export function getPackageDetail(packageId: string) {
  const pkg = findPackage(packageId);
  if (!pkg) return null;
  advancePublication(pkg);
  const editable = pkg.state === "proposed";

  return {
    package_id: pkg.package_id,
    state: pkg.state,
    package_hash: pkg.package_hash,
    expires_at: new Date(now() + 48 * HOUR).toISOString(),
    approvable: pkg.state === "proposed",
    not_approvable_reason: notApprovableReasonFor(pkg.state),
    platform: {
      code: pkg.platform_code,
      label: pkg.platform_label,
      account: { entity_ref: pkg.account_entity_ref, name: pkg.account_name },
    },
    campaign: {
      name: pkg.campaign_name,
      objective_label: pkg.objective_label,
      duration_label: pkg.duration_label,
      native_summary: pkg.native_summary,
      ad_sets: pkg.ad_sets.map((adSet) => ({
        local_ref: adSet.local_ref,
        name: adSet.name,
        node_label: adSet.node_label ?? "Conjunto de anuncios",
        audience_plain: adSet.audience_plain,
        geo_plain: adSet.geo_plain,
        schedule_plain: adSet.schedule_plain,
        keywords_plain: adSet.keywords_plain,
        keywords_note: adSet.keywords_plain ? `Se compran ${adSet.keywords_plain.length} palabras clave.` : null,
        bid_plain: adSet.bid_plain,
        asset_group: adSet.asset_group
          ? {
              business_name: adSet.asset_group.business_name,
              images: adSet.asset_group.images.map((image) => ({
                preview_url: placeholderCreativeUri(image.width, image.height, image.alt),
                width: image.width,
                height: image.height,
                alt: image.alt,
                asset_id: image.asset_id,
                policy: "ok" as const,
              })),
              headlines: adSet.asset_group.headlines,
              long_headlines: adSet.asset_group.long_headlines,
              descriptions: adSet.asset_group.descriptions,
              audience_signal_count: adSet.asset_group.audience_signal_count,
              final_url: adSet.asset_group.final_url,
            }
          : null,
        ads: adSet.ads.map((ad) => ({
          local_ref: ad.local_ref,
          name: ad.name,
          image: ad.image ? imageAssetOf(ad.image) : null,
          texts: ad.texts,
          cta_label: ad.cta_label,
          landing: { url: LANDING_URL, display: LANDING_DISPLAY },
          actions: { can_replace_image: ad.can_replace_image && editable, can_regenerate: ad.can_regenerate && editable },
        })),
      })),
    },
    money: {
      daily: { amount: pkg.money.daily, currency: "EUR" },
      monthly_equivalent: { amount: pkg.money.monthly_equivalent, currency: "EUR" },
      total_cap: { amount: pkg.money.total_cap, currency: "EUR" },
      envelope: {
        headroom: pkg.money.headroom ? { amount: pkg.money.headroom, currency: "EUR" } : null,
        reason: pkg.money.envelope_reason,
        label: pkg.money.envelope_label,
      },
      cap_label: pkg.money.cap_label,
    },
    why: pkg.why,
    on_approve: onApproveFor(pkg),
    publication: computePublicationStatus(pkg),
  };
}

/** `GET /packages/{id}/creative-candidates` — §6. */
export function getCreativeCandidates(packageId: string, adLocalRef: string) {
  const pkg = findPackage(packageId);
  if (!pkg) return null;
  const found = findAd(pkg, adLocalRef);
  if (!found?.ad.image) return { items: [] };

  const base = found.ad.image;
  return {
    items: [1, 2].map((variant) => ({
      asset_id: `${base.asset_id}_cand${variant}`,
      preview_url: placeholderCreativeUri(base.width, base.height, `${base.label} · opción ${variant}`),
      format: `${base.width}x${base.height}`,
      policy: "ok" as const,
      created_at: new Date(now() - variant * HOUR).toISOString(),
    })),
  };
}

/** Distingue visualmente el resultado en el mock — el `PATCH` real sólo recibe `creative_asset_id`. */
function labelForCreative(adName: string, creativeAssetId: string): string {
  if (creativeAssetId.includes("_cand")) return `${adName} · imagen alternativa`;
  if (creativeAssetId.endsWith("_regen")) return `${adName} · regenerada`;
  return adName;
}

type PackageErrorCode =
  | "NOT_FOUND"
  | "PACKAGE_CHANGED"
  | "PACKAGE_NOT_PROPOSED"
  | "PACKAGE_NOT_EDITABLE"
  | "PACKAGE_NOT_RESUMABLE"
  | "BRAKE_ENGAGED"
  | "UNDO_WINDOW_CLOSED";

interface PackageFail {
  ok: false;
  code: PackageErrorCode;
}

/** `PATCH /packages/{id}/ads/{ad_local_ref}/creative` — §6. Sólo editable mientras `proposed`. */
export function patchAdCreative(
  packageId: string,
  adLocalRef: string,
  packageHash: string,
  creativeAssetId: string,
): { ok: true; package_hash: string } | PackageFail {
  const pkg = findPackage(packageId);
  if (!pkg) return { ok: false, code: "NOT_FOUND" };
  if (pkg.state !== "proposed") return { ok: false, code: "PACKAGE_NOT_EDITABLE" };
  if (pkg.package_hash !== packageHash) return { ok: false, code: "PACKAGE_CHANGED" };
  const found = findAd(pkg, adLocalRef);
  if (!found) return { ok: false, code: "NOT_FOUND" };

  const [width, height] = found.ad.image ? [found.ad.image.width, found.ad.image.height] : [1200, 628];
  found.ad.image = { asset_id: creativeAssetId, label: labelForCreative(found.ad.name, creativeAssetId), width, height, policy: "ok" };
  pkg.package_hash = nextHash(pkg.package_id);
  return { ok: true, package_hash: pkg.package_hash };
}

/** `POST /packages/{id}/approve` — §4/R2.C. Idempotente por `(package_id, package_hash)` (ME-6). */
export function approvePackage(packageId: string, packageHash: string, businessId: string) {
  const pkg = findPackage(packageId);
  if (!pkg) return { ok: false, code: "NOT_FOUND" } as const;

  if (pkg.state !== "proposed" && pkg.approved_hash === packageHash && pkg.publication) {
    const pub = pkg.publication;
    return {
      ok: true as const,
      publication_id: pub.publication_id,
      authorization_id: pub.authorization_id,
      grace_seconds: pub.grace_seconds,
      execution_starts_at: new Date(pub.execution_starts_at).toISOString(),
      approval_expires_at: new Date(pub.approval_expires_at).toISOString(),
      undo: { kind: "cancel_publication" as const, deadline: new Date(pub.execution_starts_at).toISOString() },
    };
  }

  if (pkg.state !== "proposed") return { ok: false, code: "PACKAGE_NOT_PROPOSED" } as const;
  if (pkg.package_hash !== packageHash) return { ok: false, code: "PACKAGE_CHANGED" } as const;
  if (getKillSwitchState(businessId).effective.engaged) return { ok: false, code: "BRAKE_ENGAGED" } as const;

  const graceSeconds = pkg.demo_grace_seconds ?? 45;
  const executionStartsAt = now() + graceSeconds * 1000;
  pkg.state = "approved";
  pkg.approved_hash = packageHash;
  pkg.publication = {
    publication_id: nextPublicationId(),
    authorization_id: nextAuthorizationId(),
    grace_seconds: graceSeconds,
    execution_starts_at: executionStartsAt,
    approval_expires_at: now() + APPROVAL_TTL_MS,
    cursor: 0,
    fail_at_step_index: pkg.demo_fail_at_step_index ?? null,
    campaign_entity_ref: null,
    activated_at: null,
    pause_undo_deadline: null,
    halt_reason_plain: null,
    halt_next_step_plain: null,
    halt_can_resume: true,
  };

  return {
    ok: true as const,
    publication_id: pkg.publication.publication_id,
    authorization_id: pkg.publication.authorization_id,
    grace_seconds: graceSeconds,
    execution_starts_at: new Date(executionStartsAt).toISOString(),
    approval_expires_at: new Date(pkg.publication.approval_expires_at).toISOString(),
    undo: { kind: "cancel_publication" as const, deadline: new Date(executionStartsAt).toISOString() },
  };
}

/** `POST /packages/{id}/reject` — §4. */
export function rejectPackage(packageId: string, packageHash: string): { ok: true } | PackageFail {
  const pkg = findPackage(packageId);
  if (!pkg) return { ok: false, code: "NOT_FOUND" };
  if (pkg.state !== "proposed") return { ok: false, code: "PACKAGE_NOT_PROPOSED" };
  if (pkg.package_hash !== packageHash) return { ok: false, code: "PACKAGE_CHANGED" };
  pkg.state = "rejected";
  return { ok: true };
}

/** `POST /packages/{id}/resume` (Revisión 2, R2.C) — sólo desde `partially_published`. */
export function resumePackage(packageId: string, packageHash: string) {
  const pkg = findPackage(packageId);
  if (!pkg) return { ok: false, code: "NOT_FOUND" } as const;
  if (pkg.state !== "partially_published" || !pkg.publication) return { ok: false, code: "PACKAGE_NOT_RESUMABLE" } as const;
  if (pkg.package_hash !== packageHash) return { ok: false, code: "PACKAGE_CHANGED" } as const;

  // «Continuar» retoma con el mismo sobre y termina el trabajo, sin volver a fallar el mismo paso.
  pkg.publication.fail_at_step_index = null;
  pkg.publication.halt_reason_plain = null;
  pkg.publication.approval_expires_at = now() + APPROVAL_TTL_MS;
  pkg.state = "publishing";

  return {
    ok: true as const,
    publication_id: pkg.publication.publication_id,
    approval_expires_at: new Date(pkg.publication.approval_expires_at).toISOString(),
  };
}

/** `POST /packages/{id}/undo` — §5. Antes de escribir: cancela. Tras publicar: pausa. Nunca borra. */
export function undoPackage(packageId: string, packageHash: string) {
  const pkg = findPackage(packageId);
  if (!pkg) return { ok: false, code: "NOT_FOUND" } as const;
  if (pkg.package_hash !== packageHash) return { ok: false, code: "PACKAGE_CHANGED" } as const;
  const pub = pkg.publication;

  if (pkg.state === "approved" && pub && now() < pub.execution_starts_at) {
    pkg.state = "proposed";
    pkg.publication = null;
    pkg.approved_hash = null;
    return { ok: true as const, undo_kind: "cancelled_publication" as const, sentence: "Publicación cancelada. No se ha creado nada." };
  }

  if (pkg.state === "published" && pub?.pause_undo_deadline !== null && pub?.pause_undo_deadline !== undefined && now() < pub.pause_undo_deadline) {
    const campaignEntityRef = pub.campaign_entity_ref ?? `${pkg.platform_code}:campaign:${pkg.package_id}`;
    pub.pause_undo_deadline = null;
    return {
      ok: true as const,
      undo_kind: "campaign_paused" as const,
      campaign_entity_ref: campaignEntityRef,
      execution_id: `exec_pause_${pkg.package_id}`,
      sentence: "Campaña pausada. Lo creado sigue ahí, sin entregar. Puedes eliminarla en Campañas.",
    };
  }

  return { ok: false, code: "UNDO_WINDOW_CLOSED" } as const;
}

const FEED_VISIBLE_STATES = new Set<PackageLifecycleState>(["proposed", "approved", "publishing", "partially_published", "failed", "published"]);

/** «Una frase llana» (api.md §1 `why`) — la fila muestra sólo la primera del resumen del detalle. */
function oneSentence(text: string): string {
  const [first] = text.split(". ");
  return first ? `${first.replace(/\.+$/, "")}.` : text;
}

function summaryLineFor(pkg: PackageRecord): string {
  const adSetsCount = pkg.ad_sets.length;
  const adsCount = pkg.ad_sets.reduce((sum, adSet) => sum + adSet.ads.length, 0);
  const unitLabel = pkg.platform_code === "meta" ? "conjuntos" : adSetsCount === 1 ? "grupo" : "grupos";
  return `1 campaña · ${adSetsCount} ${unitLabel} · ${adsCount} anuncios · ${pkg.platform_label} · ${pkg.account_name}`;
}

/** Forma de fila de `contracts/api.md` §1 — funde con la bandeja de propuestas. */
function toPackageFeedItem(pkg: PackageRecord) {
  return {
    item_kind: "package" as const,
    package_id: pkg.package_id,
    proposal_id: null,
    action_kind: "create_package" as const,
    entity_name: pkg.campaign_name,
    platform: pkg.platform_code,
    account_name: pkg.account_name,
    headline: `Publicar la campaña «${pkg.campaign_name}»`,
    summary: summaryLineFor(pkg),
    // La fila de la bandeja usa `Money` con `amount` numérico (`api.md §1`); el detalle (§2)
    // usa cadena decimal — la conversión vive sólo aquí, en el borde de la proyección.
    money: {
      daily: { amount: Number(pkg.money.daily), currency: "EUR" },
      monthly_equivalent: { amount: Number(pkg.money.monthly_equivalent), currency: "EUR" },
      total_cap: { amount: Number(pkg.money.total_cap), currency: "EUR" },
    },
    why: oneSentence(pkg.why.summary),
    classification: pkg.classification,
    urgency: pkg.urgency,
    requires_expansion: true,
    requires_typed_confirmation: false,
    expires_at: new Date(now() + 48 * HOUR).toISOString(),
    state: pkg.state,
    diff_hash: pkg.package_hash,
  };
}

/** `GET /proposals` (`fixtures/proposals.ts`) funde estas filas en la bandeja — api.md §1. */
export function listPackageFeedItems() {
  return PACKAGES.filter((pkg) => {
    advancePublication(pkg);
    if (!FEED_VISIBLE_STATES.has(pkg.state)) return false;
    if (pkg.state === "published") {
      const deadline = pkg.publication?.pause_undo_deadline ?? null;
      return deadline !== null && now() < deadline;
    }
    return true;
  }).map(toPackageFeedItem);
}

/**
 * Regenerar reutiliza `POST /creatives/{asset_id}/regenerate` (`mocks/handlers/creatives.ts`
 * ya lo sirve para el catálogo de Creatividades). Los activos de un paquete no viven en ese
 * catálogo, así que este registro cubre el mismo verbo para IDs con prefijo `pkgcr_`/`_cand` —
 * misma ruta HTTP, un solo camino para cambiar el paquete (§6).
 */
const REGENERATE_JOBS = new Map<string, { asset_id: string; label: string; width: number; height: number }>();

export function isPackageOwnedAsset(assetId: string): boolean {
  return assetId.startsWith("pkgcr_") || assetId.includes("_cand");
}

let jobCounter = 0;
export function startRegenerateJob(assetId: string): { job_id: string } | null {
  if (!isPackageOwnedAsset(assetId)) return null;
  jobCounter += 1;
  const job_id = `pkgjob_${hashSeed(`${assetId}:${jobCounter}`).toString(16).slice(0, 10)}`;
  REGENERATE_JOBS.set(job_id, { asset_id: `${assetId}_regen`, label: "Nueva versión generada", width: 1200, height: 628 });
  return { job_id };
}

/** `GET /creative-jobs/{job_id}` — siempre `READY` en el primer sondeo: el mock es determinista para los tests. */
export function getRegenerateJob(jobId: string) {
  const job = REGENERATE_JOBS.get(jobId);
  if (!job) return null;
  return {
    job_id: jobId,
    state: "READY" as const,
    progress: 100,
    assets: [
      {
        asset_id: job.asset_id,
        business_id: "biz_ejemplo",
        label: job.label,
        media_kind: "image" as const,
        format: "1200x628" as const,
        preview_url: placeholderCreativeUri(job.width, job.height, job.label),
        signal: "LEARNING" as const,
        policy_verdict: "PASS" as const,
        policy_findings: [] as string[],
        review_state: "approved" as const,
        spend: { amount: 0, currency: "EUR" },
        hook_rate_pct: null,
        hold_rate_pct: null,
        frequency: null,
        days_in_rotation: 0,
        ads_running_on: [] as string[],
        signal_id: null,
        brief_id: null,
      },
    ],
    renderer_used: "local",
    cost_estimate: null,
  };
}

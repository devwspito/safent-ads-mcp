/**
 * Fixture con estado mutable: aprobar/rechazar/posponer/editar cambian estos registros en
 * memoria, igual que haría `ads-api`. Un mismo conjunto de propuestas se reagrupa por causa
 * (lente `urgency`) o por evento de calendario (lente `calendar_event`) — ver panel/README.md.
 * `risk_level`/`requires_expansion`/`requires_typed_confirmation` los decide este fixture
 * como haría el servidor real: el cliente sólo los lee (rest-api.md §Propuestas).
 */
import { hashSeed } from "./deterministicRandom";
import { listPackageFeedItems } from "./packages";

type Classification = "routine" | "important" | "critical";
type Urgency = "critical" | "recommended" | "minor";
type RiskLevel = "low" | "medium" | "high";
type ProposalState =
  | "pending"
  | "postponed"
  | "approved"
  | "scheduled"
  | "executing"
  | "executed"
  | "rejected"
  | "expired"
  | "invalidated"
  | "failed";

const TYPED_CONFIRMATION_PHRASE = "CONFIRMAR";

/**
 * `panel_read.py::proposal_item` vuelca en `diff.valor_propuesto` el dict
 * `{creation_plan, landing_url}` entero, serializado a JSON (`display_value` sobre un dict) —
 * nunca un número suelto. `entity_ref`/`entity_name`/`account_label` de una creación son la
 * referencia cruda de la CUENTA (no existe fila en `ad_entities` para una cuenta todavía):
 * el propio bug real que motiva este fixture (companion 0.2.20).
 */
const CREATION_LANDING_URL = "https://example.com/";
const GOOGLE_CREATION_ACCOUNT_REF =
  "google:account:5e1a6c8e-2f3d-4b7a-9c1e-8f2b6a7d4c10:9b3f2a71-6d4c-4e8a-b1f0-7c5e3a9d2f44:1000000001";
const META_CREATION_ACCOUNT_REF = "meta:act_100000000000002";

const GOOGLE_CREATION_PLAN = {
  schema_version: 1,
  platform: "google",
  name: "Acme | Primera consulta gratis | Centro Norte",
  status: "PAUSED",
  daily_budget: { amount: "10", currency: "EUR" },
  native: {
    advertising_channel_type: "SEARCH",
    bidding_strategy: "MANUAL_CPC",
    contains_eu_political_advertising: "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    network_settings: {
      target_google_search: true,
      target_search_network: false,
      target_content_network: false,
      target_partner_search_network: false,
    },
  },
};

const META_CREATION_PLAN = {
  schema_version: 1,
  platform: "meta",
  name: "Acme | Primera consulta gratis | Meta Leads",
  status: "PAUSED",
  daily_budget: { amount: "10", currency: "EUR" },
  native: {
    objective: "OUTCOME_LEADS",
    buying_type: "AUCTION",
    bid_strategy: "LOWEST_COST_WITHOUT_CAP",
    special_ad_categories: [],
    special_ad_category_country: [],
  },
};

function creationValorPropuesto(plan: Record<string, unknown>): string {
  return JSON.stringify({ creation_plan: plan, landing_url: CREATION_LANDING_URL });
}

interface ProposalRecord {
  proposal_id: string;
  entity_ref: string;
  entity_name: string;
  platform: "google" | "meta";
  account_label: string;
  /** `contracts/api.md` §1 — presente cuando la propuesta viene de un paquete ya existente. */
  package_id: string | null;
  parametro: string;
  valor_actual: number | string;
  valor_propuesto: number | string;
  /** Solo relevante cuando `parametro === "estado"` y se propone pausar (ver `toItem`). */
  current_daily_budget: number | null;
  classification: Classification;
  urgency: Urgency;
  risk_level: RiskLevel;
  requires_expansion: boolean;
  requires_typed_confirmation: boolean;
  estimated_impact: number;
  expires_in_hours: number;
  state: ProposalState;
  postponed_until: string | null;
  cause_key: string;
  cause: string;
  calendar_event_key: string;
  calendar_event_label: string;
  calendar_event_closes_at: string | null;
  rule_code: string;
  rule_hit_rate_pct: number;
  rule_hit_rate_sample: number;
  data_window: string;
  data_age_minutes: number;
  owner_context: string | null;
  execution_id: string | null;
  execution_scheduled_at: string | null;
}

function diffHashFor(proposalId: string, valorPropuesto: number | string): string {
  return `h${hashSeed(`${proposalId}:${valorPropuesto}`).toString(16).slice(0, 10)}`;
}

const now = () => Date.now();
const NO_CALENDAR_EVENT_KEY = "evt:none";
const GRACE_SECONDS_SINGLE = 20;
const GRACE_SECONDS_BATCH = 45;

const SEED: Omit<ProposalRecord, "state" | "postponed_until" | "execution_id" | "execution_scheduled_at">[] = [
  {
    proposal_id: "prop_001",
    entity_ref: "google:campaign:c-brand-search",
    entity_name: "Búsqueda Marca",
    platform: "google",
    account_label: "Google Ads — Negocio Ejemplo",
    package_id: null,
    parametro: "presupuesto_diario",
    valor_actual: 172,
    valor_propuesto: 120,
    current_daily_budget: null,
    classification: "critical",
    urgency: "critical",
    risk_level: "high",
    requires_expansion: true,
    requires_typed_confirmation: true,
    estimated_impact: -410,
    expires_in_hours: 4,
    cause_key: "cpl-sobre-umbral",
    cause: "Coste por lead 34 € frente al umbral de 25 € en 7 días",
    calendar_event_key: "evt:secundaria-madrid",
    calendar_event_label: "Secundaria Madrid",
    calendar_event_closes_at: new Date(now() + 6 * 86_400_000).toISOString(),
    rule_code: "M03",
    rule_hit_rate_pct: 78.4,
    rule_hit_rate_sample: 24,
    data_window: "7D",
    data_age_minutes: 12,
    owner_context: null,
  },
  {
    proposal_id: "prop_002",
    entity_ref: "google:campaign:c-busqueda-competencia",
    entity_name: "Búsqueda Competencia",
    platform: "google",
    account_label: "Google Ads — Negocio Ejemplo",
    package_id: null,
    parametro: "estado",
    valor_actual: "ACTIVE",
    valor_propuesto: "PAUSED",
    /** La campaña gasta 143 €/día hoy — pausarla ahorra exactamente eso, no la estimación de negocio. */
    current_daily_budget: 143,
    classification: "critical",
    urgency: "critical",
    risk_level: "high",
    requires_expansion: true,
    requires_typed_confirmation: true,
    estimated_impact: -310,
    expires_in_hours: 2,
    cause_key: "sin-conversiones-21d",
    cause: "Sin conversiones en 21 días con gasto sostenido",
    calendar_event_key: NO_CALENDAR_EVENT_KEY,
    calendar_event_label: "Sin evento de calendario",
    calendar_event_closes_at: null,
    rule_code: "X01",
    rule_hit_rate_pct: 91.2,
    rule_hit_rate_sample: 11,
    data_window: "21D",
    data_age_minutes: 18,
    owner_context: null,
  },
  {
    proposal_id: "prop_003",
    entity_ref: "meta:campaign:c-meta-advantage-catalogo",
    entity_name: "Meta Advantage+ Catálogo",
    platform: "meta",
    account_label: "Meta Ads — Negocio Ejemplo",
    package_id: null,
    parametro: "presupuesto_diario",
    valor_actual: 197,
    valor_propuesto: 221,
    current_daily_budget: null,
    classification: "routine",
    urgency: "recommended",
    risk_level: "low",
    requires_expansion: false,
    requires_typed_confirmation: false,
    estimated_impact: 180,
    expires_in_hours: 20,
    cause_key: "presupuesto-limitado-cpl-bajo",
    cause: "Limitada por presupuesto con coste por lead bajo objetivo",
    calendar_event_key: "evt:primaria-valencia",
    calendar_event_label: "Primaria Valencia",
    calendar_event_closes_at: new Date(now() + 14 * 86_400_000).toISOString(),
    rule_code: "G01",
    rule_hit_rate_pct: 84.0,
    rule_hit_rate_sample: 40,
    data_window: "7D",
    data_age_minutes: 12,
    owner_context: null,
  },
  {
    proposal_id: "prop_004",
    entity_ref: "google:campaign:c-display-retargeting",
    entity_name: "Display Retargeting",
    platform: "google",
    account_label: "Google Ads — Negocio Ejemplo",
    package_id: null,
    parametro: "presupuesto_diario",
    valor_actual: 205,
    valor_propuesto: 230,
    current_daily_budget: null,
    classification: "routine",
    urgency: "recommended",
    risk_level: "low",
    requires_expansion: false,
    requires_typed_confirmation: false,
    estimated_impact: 210,
    expires_in_hours: 20,
    cause_key: "presupuesto-limitado-cpl-bajo",
    cause: "Limitada por presupuesto con coste por lead bajo objetivo",
    calendar_event_key: "evt:primaria-valencia",
    calendar_event_label: "Primaria Valencia",
    calendar_event_closes_at: new Date(now() + 14 * 86_400_000).toISOString(),
    rule_code: "G01",
    rule_hit_rate_pct: 84.0,
    rule_hit_rate_sample: 40,
    data_window: "7D",
    data_age_minutes: 12,
    owner_context: null,
  },
  {
    proposal_id: "prop_005",
    entity_ref: "meta:campaign:c-meta-prospeccion",
    entity_name: "Meta Prospección",
    platform: "meta",
    account_label: "Meta Ads — Negocio Ejemplo",
    // Fixture para `contracts/api.md` §1: esta propuesta pertenece al paquete `pkg_meta_001`.
    package_id: "pkg_meta_001",
    parametro: "presupuesto_diario",
    valor_actual: 148,
    valor_propuesto: 166,
    current_daily_budget: null,
    classification: "routine",
    urgency: "recommended",
    risk_level: "low",
    requires_expansion: false,
    requires_typed_confirmation: false,
    estimated_impact: 150,
    expires_in_hours: 20,
    cause_key: "presupuesto-limitado-cpl-bajo",
    cause: "Limitada por presupuesto con coste por lead bajo objetivo",
    calendar_event_key: "evt:secundaria-madrid",
    calendar_event_label: "Secundaria Madrid",
    calendar_event_closes_at: new Date(now() + 6 * 86_400_000).toISOString(),
    rule_code: "G01",
    rule_hit_rate_pct: 84.0,
    rule_hit_rate_sample: 40,
    data_window: "7D",
    data_age_minutes: 12,
    owner_context: null,
  },
  {
    proposal_id: "prop_006",
    entity_ref: "meta:campaign:c-meta-lookalike",
    entity_name: "Meta Lookalike Clientes",
    platform: "meta",
    account_label: "Meta Ads — Negocio Ejemplo",
    package_id: null,
    parametro: "puja_maxima",
    valor_actual: 1.4,
    valor_propuesto: 1.55,
    current_daily_budget: null,
    classification: "routine",
    urgency: "minor",
    risk_level: "low",
    requires_expansion: false,
    requires_typed_confirmation: false,
    estimated_impact: 40,
    expires_in_hours: 40,
    cause_key: "ajuste-puja-cpl-estable",
    cause: "Ajuste menor de puja: CPL estable con margen de puja",
    calendar_event_key: NO_CALENDAR_EVENT_KEY,
    calendar_event_label: "Sin evento de calendario",
    calendar_event_closes_at: null,
    rule_code: "G04",
    rule_hit_rate_pct: 66.7,
    rule_hit_rate_sample: 9,
    data_window: "14D",
    data_age_minutes: 25,
    owner_context: null,
  },
  {
    proposal_id: "prop_007",
    entity_ref: "google:campaign:c-busqueda-generica",
    entity_name: "Búsqueda Genérica",
    platform: "google",
    account_label: "Google Ads — Negocio Ejemplo",
    package_id: null,
    parametro: "puja_maxima",
    valor_actual: 1.1,
    valor_propuesto: 1.2,
    current_daily_budget: null,
    classification: "routine",
    urgency: "minor",
    risk_level: "low",
    requires_expansion: false,
    requires_typed_confirmation: false,
    estimated_impact: 25,
    expires_in_hours: 40,
    cause_key: "ajuste-puja-cpl-estable",
    cause: "Ajuste menor de puja: CPL estable con margen de puja",
    calendar_event_key: NO_CALENDAR_EVENT_KEY,
    calendar_event_label: "Sin evento de calendario",
    calendar_event_closes_at: null,
    rule_code: "G04",
    rule_hit_rate_pct: 66.7,
    rule_hit_rate_sample: 9,
    data_window: "14D",
    data_age_minutes: 25,
    owner_context: null,
  },
  {
    // Creación de campaña real (companion 0.2.20): `entity_ref`/`entity_name`/`account_label`
    // son la referencia cruda de la cuenta a propósito — el mismo bug que corrige este cambio.
    proposal_id: "prop_create_google_001",
    entity_ref: GOOGLE_CREATION_ACCOUNT_REF,
    entity_name: GOOGLE_CREATION_ACCOUNT_REF,
    platform: "google",
    account_label: GOOGLE_CREATION_ACCOUNT_REF,
    package_id: null,
    parametro: "new_campaign:google",
    valor_actual: "null",
    valor_propuesto: creationValorPropuesto(GOOGLE_CREATION_PLAN),
    current_daily_budget: null,
    classification: "important",
    urgency: "recommended",
    risk_level: "medium",
    requires_expansion: true,
    requires_typed_confirmation: false,
    estimated_impact: 220,
    expires_in_hours: 48,
    cause_key: "creacion-acme-google",
    cause: "Nueva oportunidad de captación en Centro Norte",
    calendar_event_key: NO_CALENDAR_EVENT_KEY,
    calendar_event_label: "Sin evento de calendario",
    calendar_event_closes_at: null,
    rule_code: "NEW",
    rule_hit_rate_pct: 0,
    rule_hit_rate_sample: 0,
    data_window: "7D",
    data_age_minutes: 5,
    owner_context: null,
  },
  {
    proposal_id: "prop_create_meta_001",
    entity_ref: META_CREATION_ACCOUNT_REF,
    entity_name: META_CREATION_ACCOUNT_REF,
    platform: "meta",
    account_label: META_CREATION_ACCOUNT_REF,
    package_id: null,
    parametro: "new_campaign:meta",
    valor_actual: "null",
    valor_propuesto: creationValorPropuesto(META_CREATION_PLAN),
    current_daily_budget: null,
    classification: "important",
    urgency: "recommended",
    risk_level: "medium",
    requires_expansion: true,
    requires_typed_confirmation: false,
    estimated_impact: 260,
    expires_in_hours: 48,
    cause_key: "creacion-acme-meta",
    cause: "Nueva oportunidad de captación con Meta Ads",
    calendar_event_key: NO_CALENDAR_EVENT_KEY,
    calendar_event_label: "Sin evento de calendario",
    calendar_event_closes_at: null,
    rule_code: "NEW",
    rule_hit_rate_pct: 0,
    rule_hit_rate_sample: 0,
    data_window: "7D",
    data_age_minutes: 5,
    owner_context: null,
  },
];

const store = new Map<string, ProposalRecord>(
  SEED.map((seed) => [seed.proposal_id, { ...seed, state: "pending", postponed_until: null, execution_id: null, execution_scheduled_at: null }]),
);

export const DEFERRED_BY_PACE_COUNT = 3;
export const ATTENTION_BUDGET_LIMIT = 10;

/** Simula el trabajador: pasado el `execution_scheduled_at`, la aprobación pasa a ejecutada. */
function materializeState(record: ProposalRecord) {
  if (record.state === "approved" && record.execution_scheduled_at && now() >= new Date(record.execution_scheduled_at).getTime()) {
    record.state = "executed";
  }
}

function toDiff(record: ProposalRecord) {
  return {
    parametro: record.parametro,
    valor_actual: record.valor_actual,
    valor_propuesto: record.valor_propuesto,
    diff_hash: diffHashFor(record.proposal_id, record.valor_propuesto),
  };
}

/** Mismo criterio que `panel_read.py::proposal_item`: `is_creation = parameter.startswith("new_campaign:")`. */
function isCreationRecord(record: Pick<ProposalRecord, "parametro">): boolean {
  return record.parametro.startsWith("new_campaign:");
}

/** Mismo criterio que `panel_read.py::creation_details`: extrae `creation_plan` del payload del `diff`. */
function creationPlanFromRecord(record: ProposalRecord): Record<string, unknown> | null {
  if (!isCreationRecord(record)) return null;
  try {
    const parsed = JSON.parse(String(record.valor_propuesto)) as { creation_plan?: unknown };
    return parsed.creation_plan && typeof parsed.creation_plan === "object" ? (parsed.creation_plan as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function toItem(record: ProposalRecord) {
  materializeState(record);
  return {
    item_kind: "proposal" as const,
    package_id: record.package_id,
    action_kind: isCreationRecord(record) ? ("create_campaign" as const) : ("update" as const),
    proposal_id: record.proposal_id,
    entity_ref: record.entity_ref,
    entity_name: record.entity_name,
    platform: record.platform,
    account_label: record.account_label,
    current_daily_budget: record.current_daily_budget === null ? null : { amount: record.current_daily_budget, currency: "EUR" },
    diff: toDiff(record),
    classification: record.classification,
    urgency: record.urgency,
    risk_level: record.risk_level,
    requires_expansion: record.requires_expansion,
    requires_typed_confirmation: record.requires_typed_confirmation,
    estimated_impact: { amount: record.estimated_impact, currency: "EUR" },
    cause: record.cause,
    expires_at: new Date(now() + record.expires_in_hours * 3_600_000).toISOString(),
    postponed_until: record.postponed_until,
    state: record.state,
  };
}

function activeRecords(): ProposalRecord[] {
  return [...store.values()].filter((r) => {
    materializeState(r);
    return r.state === "pending" || r.state === "postponed";
  });
}

type FeedItem = ReturnType<typeof toItem> | ReturnType<typeof listPackageFeedItems>[number];
interface FeedGroup {
  group_kind: string;
  cause_key: string;
  cause: string;
  count: number;
  total_impact: { amount: number; currency: string };
  batch_eligible: boolean;
  closes_at: string | null;
  proposals: FeedItem[];
}

export function listProposalGroups(lens: "urgency" | "calendar_event") {
  const records = activeRecords();
  const groupKind = lens === "urgency" ? "cause" : "calendar_event";
  const groupKeyField = lens === "urgency" ? "cause_key" : "calendar_event_key";
  const groupLabelField = lens === "urgency" ? "cause" : "calendar_event_label";

  const byKey = new Map<string, ProposalRecord[]>();
  for (const record of records) {
    const key = record[groupKeyField];
    const list = byKey.get(key) ?? [];
    list.push(record);
    byKey.set(key, list);
  }

  const groups: FeedGroup[] = [...byKey.entries()].map(([key, items]) => {
    const first = items[0]!;
    const totalImpact = items.reduce((sum, item) => sum + item.estimated_impact, 0);
    const sameCause = items.every((item) => item.cause_key === first.cause_key);
    const allLowRisk = items.every((item) => item.risk_level === "low");
    return {
      group_kind: groupKind,
      cause_key: key,
      cause: first[groupLabelField] ?? key,
      count: items.length,
      total_impact: { amount: Math.round(totalImpact * 100) / 100, currency: "EUR" },
      batch_eligible: items.length > 1 && sameCause && allLowRisk,
      closes_at: lens === "calendar_event" ? first.calendar_event_closes_at : null,
      proposals: items.map(toItem),
    };
  });

  // api.md §1: un paquete es siempre su propio grupo de una fila — nunca `batch_eligible`,
  // nunca comparte causa con una propuesta normal. En la lente calendar_event mantiene el
  // mismo prefijo `evt:` que el resto de grupos (no tiene evento propio, así que no cuenta
  // para el cierre más próximo).
  const packageGroups = listPackageFeedItems().map((item) => ({
    group_kind: groupKind,
    cause_key: lens === "calendar_event" ? `evt:pkg:${item.package_id}` : `pkg:${item.package_id}`,
    cause: item.headline,
    count: 1,
    total_impact: { amount: item.money.daily.amount, currency: item.money.daily.currency },
    batch_eligible: false,
    closes_at: null,
    proposals: [item],
  }));

  if (lens === "calendar_event") {
    groups.sort((a, b) => {
      if (a.cause_key === NO_CALENDAR_EVENT_KEY) return 1;
      if (b.cause_key === NO_CALENDAR_EVENT_KEY) return -1;
      return new Date(a.closes_at ?? 0).getTime() - new Date(b.closes_at ?? 0).getTime();
    });
    // "Sin evento de calendario" se queda el último de todos — los paquetes se insertan justo
    // antes, o al final si no hubiera ningún grupo sin evento.
    const noEventIndex = groups.findIndex((group) => group.cause_key === NO_CALENDAR_EVENT_KEY);
    if (noEventIndex === -1) groups.push(...packageGroups);
    else groups.splice(noEventIndex, 0, ...packageGroups);
  } else {
    groups.push(...packageGroups);
    const order: Record<Urgency, number> = { critical: 0, recommended: 1, minor: 2 };
    groups.sort((a, b) => (order[a.proposals[0]?.urgency ?? "minor"] ?? 3) - (order[b.proposals[0]?.urgency ?? "minor"] ?? 3));
  }

  const allProposals = groups.flatMap((g) => g.proposals);
  // Un paquete no lleva `estimated_impact` (usa `money.daily` en su lugar, api.md §1) — no cuenta
  // para el total de impacto de la bandeja, que es sólo de propuestas normales.
  const totalImpact = allProposals.reduce((sum, p) => sum + ("estimated_impact" in p ? p.estimated_impact.amount : 0), 0);
  const nextExpiring = allProposals.reduce<string | null>((soonest, p) => {
    if (!soonest || new Date(p.expires_at) < new Date(soonest)) return p.expires_at;
    return soonest;
  }, null);

  return {
    lens,
    pending_count: allProposals.length,
    deferred_count: DEFERRED_BY_PACE_COUNT,
    total_impact: { amount: Math.round(totalImpact * 100) / 100, currency: "EUR" },
    next_expiring_at: nextExpiring,
    attention_budget: { used: Math.min(allProposals.length, ATTENTION_BUDGET_LIMIT), limit: ATTENTION_BUDGET_LIMIT },
    groups,
    next_cursor: null,
  };
}

export function getProposalDetail(proposalId: string) {
  const record = store.get(proposalId);
  if (!record) return null;
  materializeState(record);

  const magnitude = Math.abs(Number(record.estimated_impact));
  const sign = Math.sign(record.estimated_impact || 1);
  const isCreation = isCreationRecord(record);
  return {
    ...toItem(record),
    creation_plan: creationPlanFromRecord(record),
    creation_plan_error: null,
    cause_key: record.cause_key,
    signal_id: `sig_${record.proposal_id}`,
    // Una creación no la disparó ninguna regla ni señal de cartera — nunca se inventa una.
    signal: isCreation ? null : { kind: record.estimated_impact < 0 ? "SELL" : "BUY", strength: Math.round(record.rule_hit_rate_pct), cause: record.cause },
    rule_id: isCreation ? null : `rule_${record.rule_code}`,
    rule_code: isCreation ? null : record.rule_code,
    rule_hit_rate_pct: isCreation ? null : record.rule_hit_rate_pct,
    rule_hit_rate_sample: isCreation ? 0 : record.rule_hit_rate_sample,
    data_window: record.data_window,
    data_age_minutes: record.data_age_minutes,
    estimated_impact_range: {
      low: { amount: Math.round(magnitude * 0.8) * sign, currency: "EUR" },
      high: { amount: Math.round(magnitude * 1.2) * sign, currency: "EUR" },
    },
    // Sin histórico de rendimiento para una campaña que todavía no existe.
    evidence: isCreation ? [] : [
      {
        metric: record.parametro === "estado" ? "business_conversions" : "coste_por_lead",
        unit: record.parametro === "estado" ? ("count" as const) : ("money" as const),
        target: record.parametro === "estado" ? 1 : 25,
        window: record.data_window,
        data_age_minutes: record.data_age_minutes,
        series: Array.from({ length: 7 }, (_, i) => ({
          date: new Date(now() - (6 - i) * 86_400_000).toISOString().slice(0, 10),
          value: typeof record.valor_actual === "number" ? Math.round(record.valor_actual * (0.9 + i * 0.02) * 100) / 100 : 0,
        })),
      },
    ],
    // Los límites de seguridad se validan al aprobar/ejecutar, nunca antes de que exista la campaña.
    guardrail_verdicts: isCreation ? [] : [
      {
        guardrail_id: "gr_business",
        name: "Tope diario",
        current: Number(record.valor_propuesto) || 0,
        limit: 600,
        unit: "money" as const,
        ok: Number(record.valor_propuesto) <= 600,
      },
      { guardrail_id: "gr_business", name: "Salto máximo por cambio", current: 20, limit: 20, unit: "percent" as const, ok: true },
      { guardrail_id: "gr_business", name: "Cambios hoy en la campaña", current: 1, limit: 2, unit: "count" as const, ok: true },
    ],
    // Una campaña que todavía no existe no trae historial previo.
    entity_history: isCreation ? [] : [
      {
        at: new Date(now() - 2 * 86_400_000).toISOString(),
        actor_kind: "owner" as const,
        actor_label: "Dueño",
        summary: "Aprobó subida de presupuesto +15 %",
        proposal_id: null,
      },
      {
        at: new Date(now() - 9 * 86_400_000).toISOString(),
        actor_kind: "rule_engine" as const,
        actor_label: `Regla ${record.rule_code}`,
        summary: "Bajada automática por CPL sobre umbral",
        proposal_id: null,
      },
    ],
    owner_context: record.owner_context,
    execution_id: record.execution_id,
    /** El servidor construye esta URL desde entity_ref (C-11); una cuenta sin campaña todavía no tiene nada que abrir. */
    platform_url: isCreation ? null : `https://ads.example/${record.platform}/${encodeURIComponent(record.entity_ref)}`,
  };
}

export function setOwnerContext(proposalId: string, text: string): boolean {
  const record = store.get(proposalId);
  if (!record) return false;
  record.owner_context = text.slice(0, 500);
  return true;
}

type ApproveOutcome =
  | { ok: true; authorization_id: string; execution_id: string; execution_scheduled_at: string; undo_deadline: string; grace_seconds: number }
  | { ok: false; code: "NOT_FOUND" | "DIFF_CHANGED" | "PROPOSAL_EXPIRED" }
  | { ok: false; code: "TYPED_CONFIRMATION_REQUIRED"; phrase: string };

export function approveProposal(proposalId: string, diffHash: string, typedConfirmation?: string, graceSeconds = GRACE_SECONDS_SINGLE): ApproveOutcome {
  const record = store.get(proposalId);
  if (!record) return { ok: false, code: "NOT_FOUND" };
  materializeState(record);
  if (record.state !== "pending" && record.state !== "postponed") return { ok: false, code: "PROPOSAL_EXPIRED" };
  if (diffHashFor(proposalId, record.valor_propuesto) !== diffHash) return { ok: false, code: "DIFF_CHANGED" };
  if (record.requires_typed_confirmation && typedConfirmation?.trim().toUpperCase() !== TYPED_CONFIRMATION_PHRASE) {
    return { ok: false, code: "TYPED_CONFIRMATION_REQUIRED", phrase: TYPED_CONFIRMATION_PHRASE };
  }

  record.state = "approved";
  const executionId = `exec_${proposalId}`;
  const scheduledAt = new Date(now() + graceSeconds * 1_000).toISOString();
  record.execution_id = executionId;
  record.execution_scheduled_at = scheduledAt;

  return {
    ok: true,
    authorization_id: `auth_${proposalId}`,
    execution_id: executionId,
    execution_scheduled_at: scheduledAt,
    undo_deadline: scheduledAt,
    grace_seconds: graceSeconds,
  };
}

export function rejectProposal(proposalId: string, diffHash: string) {
  const record = store.get(proposalId);
  if (!record) return { ok: false as const, code: "NOT_FOUND" as const };
  if (diffHashFor(proposalId, record.valor_propuesto) !== diffHash) return { ok: false as const, code: "DIFF_CHANGED" as const };
  record.state = "rejected";
  return { ok: true as const };
}

export function postponeProposal(proposalId: string, until: string) {
  const record = store.get(proposalId);
  if (!record) return { ok: false as const };
  record.state = "postponed";
  record.postponed_until = until;
  return { ok: true as const };
}

export function patchProposalValue(proposalId: string, valorPropuesto: number) {
  const record = store.get(proposalId);
  if (!record) return null;
  record.valor_propuesto = valorPropuesto;
  record.state = "pending";
  return { diff_hash: diffHashFor(proposalId, valorPropuesto) };
}

export function batchApprove(causeKey: string, items: Array<{ proposal_id: string; diff_hash: string }>) {
  const results = items.map(({ proposal_id, diff_hash }) => {
    const record = store.get(proposal_id);
    if (!record || record.cause_key !== causeKey) return { proposal_id, ok: false, error_code: "NOT_IN_GROUP" };
    const result = approveProposal(proposal_id, diff_hash, undefined, GRACE_SECONDS_BATCH);
    if (!result.ok) return { proposal_id, ok: false, error_code: result.code };
    return { proposal_id, ok: true, execution_id: result.execution_id, execution_scheduled_at: result.execution_scheduled_at };
  });

  const okResults = results.filter((r) => r.ok);
  return {
    approved_count: okResults.length,
    failed_count: results.length - okResults.length,
    grace_seconds: GRACE_SECONDS_BATCH,
    execution_ids: okResults.map((r) => r.execution_id).filter((id): id is string => Boolean(id)),
    results,
  };
}

function recordByExecutionId(executionId: string): ProposalRecord | undefined {
  return [...store.values()].find((r) => r.execution_id === executionId);
}

/** Demo read model; never used by the production execution path. */
export function getExecution(executionId: string) {
  const record = recordByExecutionId(executionId);
  if (!record || !record.execution_scheduled_at) return null;
  materializeState(record);
  const succeeded = record.state === "executed";
  return {
    execution_id: executionId,
    proposal_id: record.proposal_id,
    entity_name: record.entity_name,
    outcome: succeeded ? "SUCCEEDED" : "CLAIMED",
    error_code: null,
    applied_value: succeeded ? record.valor_propuesto : null,
    previous_value: record.valor_actual,
    estimated_impact: { amount: record.estimated_impact, currency: "EUR" },
    undo_deadline: null,
    started_at: record.execution_scheduled_at,
    finished_at: succeeded ? record.execution_scheduled_at : null,
    undone_at: null,
    compensating_proposal_id: null,
  };
}

export function undoExecution(executionId: string) {
  const record = recordByExecutionId(executionId);
  if (!record) return { ok: false as const, code: "NOT_FOUND" as const };
  materializeState(record);
  if (record.state === "executed") {
    // Ya ejecutada: compensa con una propuesta de restauración en vez de cancelar.
    const compensatingId = `prop_undo_${record.proposal_id}`;
    record.state = "invalidated";
    return { ok: true as const, undo_kind: "compensated" as const, execution_id: executionId, compensating_proposal_id: compensatingId };
  }
  if (record.state !== "approved") return { ok: false as const, code: "UNDO_WINDOW_CLOSED" as const };
  record.state = "rejected";
  record.execution_id = null;
  record.execution_scheduled_at = null;
  return { ok: true as const, undo_kind: "cancelled" as const, execution_id: executionId, compensating_proposal_id: null };
}

export function undoExecutionsBatch(executionIds: string[]) {
  return executionIds.map((executionId) => {
    const result = undoExecution(executionId);
    if (!result.ok) return { execution_id: executionId, ok: false, error_code: result.code };
    return { execution_id: executionId, ok: true, undo_kind: result.undo_kind };
  });
}

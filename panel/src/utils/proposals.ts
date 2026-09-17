/**
 * Lógica pura de propuestas — portada de `oposads-agent/panel/src/utils/proposals.ts`
 * y adaptada al contrato de `contracts/rest-api.md` §Propuestas y aprobación.
 *
 * `risk_level`, `requires_expansion` y `requires_typed_confirmation` los decide el
 * SERVIDOR (rest-api.md §Propuestas: "la fricción es política, y panel y Telegram
 * aplican la misma") — este módulo sólo traduce a español, nunca deriva fricción.
 */
import { z } from "zod";
import type { Money } from "@/api/schemas";
import { isPackageFeedItem, type PackageFeedItem, type ProposalClassification, type ProposalDiff, type ProposalItem, type ProposalRiskLevel, type ProposalState, type ProposalUrgency } from "@/api/schemas/proposals";
import { campaignCreationSchema, type CampaignCreationPlan, type CampaignObjective, type GoogleAdvertisingChannelType, type GoogleBiddingStrategyPlan, type GoogleCampaignNative } from "@/api/schemas/campaignCreation";
import { formatMoney } from "@/utils/money";

/** El "id de fila" de la bandeja: `proposal_id` para una propuesta, `package_id` para un paquete (api.md §1). */
export function feedItemId(item: ProposalItem | PackageFeedItem): string {
  return isPackageFeedItem(item) ? item.package_id : item.proposal_id;
}

const urgencyLabels: Record<ProposalUrgency, string> = {
  critical: "Crítico",
  recommended: "Recomendado",
  minor: "Menor",
};

export function urgencyLabel(urgency: ProposalUrgency): string {
  return urgencyLabels[urgency];
}

const classificationLabels: Record<ProposalClassification, string> = {
  routine: "Rutinaria",
  important: "Importante",
  critical: "Crítica",
};

export function classificationLabel(classification: ProposalClassification): string {
  return classificationLabels[classification];
}

const riskLevelLabels: Record<ProposalRiskLevel, string> = {
  low: "Riesgo bajo",
  medium: "Riesgo medio",
  high: "Riesgo alto",
};

export function riskLevelLabel(risk: ProposalRiskLevel): string {
  return riskLevelLabels[risk];
}

const campaignObjectiveLabels: Record<CampaignObjective, string> = {
  OUTCOME_AWARENESS: "Reconocimiento",
  OUTCOME_ENGAGEMENT: "Interacción",
  OUTCOME_LEADS: "Clientes potenciales",
  OUTCOME_SALES: "Ventas",
  OUTCOME_TRAFFIC: "Tráfico",
  OUTCOME_APP_PROMOTION: "Promoción de aplicaciones",
};

export function campaignObjectiveLabel(objective: CampaignObjective): string {
  return campaignObjectiveLabels[objective];
}

const googleChannelTypeLabels: Record<GoogleAdvertisingChannelType, string> = {
  SEARCH: "Google Search",
  DISPLAY: "Google Display",
  DEMAND_GEN: "Demand Gen",
  PERFORMANCE_MAX: "Performance Max",
};

export function googleChannelTypeLabel(channel: GoogleAdvertisingChannelType): string {
  return googleChannelTypeLabels[channel];
}

function googleBiddingObjectLabel(bidding: GoogleBiddingStrategyPlan): string {
  switch (bidding.kind) {
    case "MANUAL_CPC":
      return "CPC manual";
    case "MAXIMIZE_CLICKS":
      return bidding.cpc_bid_ceiling ? `Maximizar clics (techo ${bidding.cpc_bid_ceiling.amount} EUR)` : "Maximizar clics";
    case "MAXIMIZE_CONVERSIONS":
      return bidding.target_cpa ? `Maximizar conversiones (CPA objetivo ${bidding.target_cpa.amount} EUR)` : "Maximizar conversiones";
    case "MAXIMIZE_CONVERSION_VALUE":
      return bidding.target_roas ? `Maximizar el valor de conversión (ROAS objetivo ${bidding.target_roas})` : "Maximizar el valor de conversión";
  }
}

/** `«CPC manual», «Maximizar clics»…` — la cifra opcional de la puja, si el plan la trae, en el mismo texto. */
export function googleBiddingStrategyLabel(native: GoogleCampaignNative): string {
  const bidding = native.bidding_strategy;
  return typeof bidding === "string" ? "CPC manual" : googleBiddingObjectLabel(bidding);
}

/** Identificadores de `geoTargetConstants/…` tal cual llegan — nunca se inventa un nombre de zona. */
export function googleGeographicTargetingLabel(native: GoogleCampaignNative): string | null {
  return native.geographic_targeting ? native.geographic_targeting.geo_target_constants.join(", ") : null;
}

/** Nombres de recurso de conversión de Google Ads tal cual llegan — sin resolver a un nombre legible. */
export function googleConversionGoalsLabel(native: GoogleCampaignNative): string | null {
  const goals = native.conversion_goals;
  return goals && goals.length ? goals.map((goal) => goal.resource_name).join(", ") : null;
}

const stateLabels: Record<ProposalState, string> = {
  pending: "Pendiente",
  postponed: "Pospuesta",
  approved: "Aprobada",
  scheduled: "Programada",
  executing: "Ejecutando",
  executed: "Ejecutada",
  rejected: "Rechazada",
  expired: "Caducada",
  invalidated: "Invalidada",
  failed: "Fallida",
};

export function proposalStateLabel(state: string): string {
  return (stateLabels as Record<string, string>)[state] ?? state;
}

/** Devuelve una fecha ISO en el futuro para las opciones rápidas de posponer. */
export function quickPostponeDate(option: "tarde" | "manana" | "2dias" | "semana"): string {
  const now = new Date();
  const local = new Date(now);

  switch (option) {
    case "tarde": {
      local.setHours(18, 0, 0, 0);
      if (local <= now) {
        local.setDate(local.getDate() + 1);
        local.setHours(9, 0, 0, 0);
      }
      break;
    }
    case "manana": {
      local.setDate(local.getDate() + 1);
      local.setHours(9, 0, 0, 0);
      break;
    }
    case "2dias": {
      local.setDate(local.getDate() + 2);
      local.setHours(9, 0, 0, 0);
      break;
    }
    case "semana": {
      local.setDate(local.getDate() + 7);
      local.setHours(9, 0, 0, 0);
      break;
    }
  }

  return local.toISOString();
}

const POSTPONE_OPTION_LABELS: Record<"tarde" | "manana" | "2dias" | "semana", string> = {
  tarde: "Esta tarde",
  manana: "Mañana",
  "2dias": "En 2 días",
  semana: "En una semana",
};

export function postponeOptionLabel(option: keyof typeof POSTPONE_OPTION_LABELS): string {
  return POSTPONE_OPTION_LABELS[option];
}

/** Minutos hasta la caducidad; negativo si ya caducó. */
export function minutesUntil(isoDate: string, now: Date = new Date()): number {
  return Math.round((new Date(isoDate).getTime() - now.getTime()) / 60_000);
}

/** `caduca en 3 h`, `caduca en 12 min`, `caducada hace 1 h` — panel-interaction-spec.md §7. */
export function expiryCountdownLabel(isoDate: string, now: Date = new Date()): string {
  const minutes = minutesUntil(isoDate, now);
  if (minutes <= 0) return `caducada hace ${formatDuration(-minutes)}`;
  return `caduca en ${formatDuration(minutes)}`;
}

function formatDuration(minutes: number): string {
  if (minutes < 60) return `${Math.max(minutes, 1)} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h`;
  return `${Math.round(hours / 24)} d`;
}

/** `cierra en 6 d` — el grupo de la lente calendar_event trae `closes_at` separado de la etiqueta (rest-api.md §Propuestas). */
export function calendarEventClosesLabel(closesAt: string, now: Date = new Date()): string {
  const days = Math.max(0, Math.ceil((new Date(closesAt).getTime() - now.getTime()) / 86_400_000));
  return `cierra en ${days} d`;
}

/** Solo se enseña la caducidad si faltan menos de 24 h — panel-interaction-spec.md §3 punto 5. */
export function expiresWithin24h(isoDate: string, now: Date = new Date()): boolean {
  return minutesUntil(isoDate, now) < 24 * 60;
}

const PARAMETRO_LABELS: Record<string, string> = {
  presupuesto_diario: "el presupuesto",
  puja_maxima: "la puja máxima",
};

/**
 * Forma del `diff` de una propuesta `create_campaign`: el propio plan de creación (y, junto a
 * él, el destino guardado) viajan serializados a JSON en `diff.valor_propuesto` — el servidor
 * no tiene otro sitio donde ponerlos («un diff = una escritura», data-model.md §1) y por eso
 * `panel_read.py::proposal_item` vuelca ahí el dict `{creation_plan, landing_url}` entero.
 */
const creationDiffPayloadSchema = z
  .object({ creation_plan: z.unknown().optional(), landing_url: z.string().nullable().optional() })
  .passthrough();

/** Sólo lo mínimo para enseñar dinero cuando el plan entero no valida — nunca sustituye al contrato completo. */
const rawDailyBudgetSchema = z
  .object({ daily_budget: z.object({ amount: z.string(), currency: z.string().length(3) }).passthrough() })
  .passthrough();

export interface CreationDiffPayload {
  plan: CampaignCreationPlan | null;
  landingUrl: string | null;
  /** `creation_plan.daily_budget` sin pasar por `campaignCreationSchema` — para no enseñar "Sin coste extra" cuando el plan trae coste pero un canal/forma que el esquema todavía no cubre. */
  rawDailyBudget: Money | null;
}

/**
 * Lee el plan de creación (y su destino) desde `diff.valor_propuesto` para pintar la fila
 * *antes* de expandir el detalle — nunca desde `entity_name`/`entity_ref`, que para una
 * cuenta sin campaña todavía son la referencia cruda (panel_read.py `_entity_names`: sin fila
 * en `ad_entities`, cae al propio `entity_ref`). Un parseo fallido nunca lanza: la fila cae al
 * texto genérico, nunca a la referencia cruda.
 */
export function creationDiffPayload(diff: Pick<ProposalDiff, "valor_propuesto">): CreationDiffPayload {
  if (typeof diff.valor_propuesto !== "string") return { plan: null, landingUrl: null, rawDailyBudget: null };
  let raw: unknown;
  try {
    raw = JSON.parse(diff.valor_propuesto);
  } catch {
    return { plan: null, landingUrl: null, rawDailyBudget: null };
  }
  const parsed = creationDiffPayloadSchema.safeParse(raw);
  if (!parsed.success) return { plan: null, landingUrl: null, rawDailyBudget: null };
  const plan = campaignCreationSchema.safeParse(parsed.data.creation_plan);
  const rawBudget = rawDailyBudgetSchema.safeParse(parsed.data.creation_plan);
  return {
    plan: plan.success ? plan.data : null,
    landingUrl: parsed.data.landing_url ?? null,
    rawDailyBudget: rawBudget.success ? { amount: Number(rawBudget.data.daily_budget.amount), currency: rawBudget.data.daily_budget.currency } : null,
  };
}

/**
 * La frase con verbo y cifras que encabeza cada fila — panel-interaction-spec.md §3:
 * "Subir el presupuesto de 30 € a 45 € al día". `risk_level`/`requires_*` los decide el
 * servidor; esta función solo traduce el `diff` ya recibido a lenguaje llano, nunca infiere
 * fricción ni importes nuevos.
 */
export function describeProposalChange(proposal: Pick<ProposalItem, "action_kind" | "entity_name" | "diff">): string {
  if (proposal.action_kind === "create_campaign") {
    const plan = creationDiffPayload(proposal.diff).plan;
    return plan ? `Crear la campaña «${plan.name}»` : "Crear una campaña nueva";
  }
  return describeDiff(proposal.diff);
}

/**
 * El nombre que se enseña junto a la plataforma y la cuenta (design.md §2.2). Para una
 * creación, `entity_name` es la referencia cruda de la cuenta (mismo motivo que arriba):
 * el nombre real vive en el plan de creación, nunca en `entity_name`.
 */
export function proposalDisplayName(proposal: Pick<ProposalItem, "action_kind" | "entity_name" | "diff">): string {
  if (proposal.action_kind !== "create_campaign") return proposal.entity_name;
  return creationDiffPayload(proposal.diff).plan?.name ?? "Campaña nueva";
}

function describeDiff(diff: ProposalDiff): string {
  const { parametro, valor_actual, valor_propuesto } = diff;

  if (parametro === "estado") {
    if (valor_propuesto === "PAUSED") return "Pausar la campaña";
    if (valor_propuesto === "ACTIVE") return "Reanudar la campaña";
    return `Cambiar el estado a ${valor_propuesto}`;
  }

  if (typeof valor_actual === "number" && typeof valor_propuesto === "number") {
    const currency = diff.currency ?? "EUR";
    const from = formatMoney({ amount: valor_actual, currency });
    const to = formatMoney({ amount: valor_propuesto, currency });
    const verb = valor_propuesto > valor_actual ? "Subir" : "Bajar";
    const label = PARAMETRO_LABELS[parametro] ?? parametro.replace(/_/g, " ");
    const suffix = parametro === "presupuesto_diario" ? " al día" : "";
    return `${verb} ${label} de ${from} a ${to}${suffix}`;
  }

  return `Cambiar ${parametro.replace(/_/g, " ")} de ${valor_actual} a ${valor_propuesto}`;
}

type ProposalForMoney = Pick<ProposalItem, "action_kind" | "diff" | "estimated_impact" | "current_daily_budget">;

/**
 * El dinero real del cambio, nunca el «impacto estimado» (una predicción de negocio que vive
 * aparte, en el detalle, como «Efecto estimado») — design.md §3 fix (a):
 * - Cambio de presupuesto: la diferencia literal propuesto − actual, al día.
 * - Pausar: menos el presupuesto diario vigente (lo que deja de gastarse).
 * - Crear/publicar: el presupuesto diario propuesto, o `null` si no añade coste (→ "Sin coste extra").
 * `null` también cuando la acción no tiene una cifra diaria clara (p. ej. una puja).
 */
export function proposalDailyMoney(proposal: ProposalForMoney): Money | null {
  const { parametro, valor_actual, valor_propuesto, currency } = proposal.diff;
  const fallbackCurrency = currency ?? proposal.estimated_impact.currency;

  if (proposal.action_kind === "create_campaign") {
    const { plan, rawDailyBudget } = creationDiffPayload(proposal.diff);
    if (plan) return { amount: Number(plan.daily_budget.amount), currency: plan.daily_budget.currency };
    // The full plan can fail `campaignCreationSchema` (an unrecognized channel/bidding shape,
    // a future server change) while its budget is still a plain, trustworthy number — showing
    // "Sin coste extra" for a campaign that plainly costs money is the worse failure mode.
    if (rawDailyBudget) return rawDailyBudget;
    if (typeof valor_propuesto === "number") return { amount: valor_propuesto, currency: fallbackCurrency };
    return { amount: 0, currency: fallbackCurrency };
  }

  if (parametro === "estado" && valor_propuesto === "PAUSED") {
    const budget = proposal.current_daily_budget ?? proposal.estimated_impact;
    return { amount: -Math.abs(budget.amount), currency: budget.currency };
  }

  if (parametro === "presupuesto_diario" && typeof valor_actual === "number" && typeof valor_propuesto === "number") {
    return { amount: Math.round((valor_propuesto - valor_actual) * 100) / 100, currency: fallbackCurrency };
  }

  return null;
}

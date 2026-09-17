import type { SignalRow, SignalsResponse } from "@/api/schemas";
import { CAMPAIGN_ENTITIES as ENTITIES } from "./campaignEntities";
import { makeRng, pickFrom } from "./deterministicRandom";

const EUR = "EUR";

const KIND_POOL = ["BUY", "HOLD", "SELL", "EXIT", "FATIGUE", "WINNER", "LOSER", "ANOMALY"] as const;

const CAUSES: Record<string, string[]> = {
  BUY: ["Coste por conversión un 18 % bajo el objetivo en 14 días", "Ritmo de conversión sostenido con margen de presupuesto"],
  HOLD: ["Dentro de rango: sin desviación relevante en 7 días"],
  SELL: ["Coste por lead 34 € frente al umbral de 25 € en 7 días", "Frecuencia sobre 4 con caída de CTR en 5 días"],
  EXIT: ["Sin conversiones en 21 días con gasto sostenido", "Coste por lead duplica el umbral en 10 días"],
  FATIGUE: ["Frecuencia 5,2 con hook rate a la mitad en 5 días"],
  WINNER: ["Hold rate un 40 % sobre la media de la cuenta en 14 días"],
  LOSER: ["Hook rate bajo el 8 % desde el día 3 de rotación"],
  ANOMALY: ["Gasto 3,1× la media diaria en la última hora"],
};

/** Sólo BUY/HOLD/SELL/EXIT tienen contraste a 14 días; FATIGUE/WINNER/LOSER/ANOMALY son `not_applicable`. */
const NOT_APPLICABLE_KINDS = new Set(["FATIGUE", "WINNER", "LOSER", "ANOMALY"]);
const FINAL_KINDS = new Set(["SELL", "EXIT"]);

const ACTIONS = ["autonomous", "proposal", "none"] as const;
const RESOLVED_OUTCOME_STATUSES = ["confirmed", "not_confirmed"] as const;

function buildSignal(index: number): SignalRow {
  const rng = makeRng(`signal-${index}`);
  const entity = pickFrom(ENTITIES, rng());
  const kind = pickFrom(KIND_POOL, rng());
  const strength = Math.floor(rng() * 100);
  const causesForKind = CAUSES[kind] ?? ["Evaluación periódica del ciclo"];
  const cause = pickFrom(causesForKind, rng());
  const emittedHoursAgo = Math.floor(rng() * 20 * 24);
  const daysSinceEmission = emittedHoursAgo / 24;
  const emittedAt = new Date(Date.now() - emittedHoursAgo * 3_600_000).toISOString();
  const actionTaken = pickFrom(ACTIONS, rng());

  let outcome: SignalRow["outcome"];
  if (NOT_APPLICABLE_KINDS.has(kind) || kind === "HOLD") {
    outcome = { status: "not_applicable", days_remaining: null, evaluated_at: null };
  } else if (!FINAL_KINDS.has(kind)) {
    outcome = { status: "pending", days_remaining: null, evaluated_at: null };
  } else if (daysSinceEmission >= 14) {
    const status = pickFrom(RESOLVED_OUTCOME_STATUSES, rng());
    outcome = { status, days_remaining: null, evaluated_at: new Date(Date.now() - 1 * 3_600_000).toISOString() };
  } else {
    outcome = { status: "in_progress", days_remaining: Math.ceil(14 - daysSinceEmission), evaluated_at: null };
  }

  return {
    signal_id: `sig_${index.toString().padStart(4, "0")}`,
    entity_ref: entity.ref,
    entity_name: entity.name,
    platform: entity.platform,
    business_id: "biz_ejemplo",
    kind,
    strength,
    cause,
    data_window: "7D",
    money_at_stake: { amount: Math.round(rng() * 500 * 100) / 100, currency: EUR },
    action_taken: actionTaken,
    proposal_id: actionTaken === "proposal" ? `prop_sig_${index}` : null,
    emitted_at: emittedAt,
    outcome,
  };
}

const ALL_SIGNALS: SignalRow[] = Array.from({ length: 42 }, (_, i) => buildSignal(i)).sort(
  (a, b) => new Date(b.emitted_at).getTime() - new Date(a.emitted_at).getTime(),
);

export interface SignalsFixtureFilters {
  platform?: string;
  kind?: string;
  min_strength?: number;
}

export function buildSignalsResponse(filters: SignalsFixtureFilters): SignalsResponse {
  const items = ALL_SIGNALS.filter((signal) => {
    if (filters.platform && signal.platform !== filters.platform) return false;
    if (filters.kind && signal.kind !== filters.kind) return false;
    if (filters.min_strength !== undefined && signal.strength < filters.min_strength) return false;
    return true;
  });

  const resolved = items.filter((s) => s.outcome.status === "confirmed" || s.outcome.status === "not_confirmed");
  const confirmed = resolved.filter((s) => s.outcome.status === "confirmed").length;

  return {
    items,
    next_cursor: null,
    /** La cabecera no muestra tasa con muestra <10 — panel-visual: el filtro lo decide el consumidor. */
    confirmed_rate_pct: resolved.length > 0 ? Math.round((confirmed / resolved.length) * 1000) / 10 : null,
    confirmed_rate_sample: resolved.length,
  };
}

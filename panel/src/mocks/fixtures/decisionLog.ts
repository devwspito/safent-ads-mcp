import { CAMPAIGN_ENTITIES } from "./campaignEntities";

const e = (i: number) => CAMPAIGN_ENTITIES[i % CAMPAIGN_ENTITIES.length]!;
const now = () => Date.now();
const hoursAgo = (h: number) => new Date(now() - h * 3_600_000).toISOString();

interface LogSeed {
  event_type:
    | "SignalEmitted"
    | "ProposalRaised"
    | "ProposalApproved"
    | "ProposalRejected"
    | "ProposalExpired"
    | "ExecutionSucceeded"
    | "ExecutionFailed"
    | "ExecutionUndone"
    | "RuleFired"
    | "EmergencyBrakeEngaged"
    | "EmergencyBrakeReleased"
    | "PlatformAccountSuspended"
    | "AdEntityDrifted";
  entity_index: number | null;
  actor_kind: "owner" | "rule_engine" | "agent" | "system";
  actor_label: string;
  proposal_id: string | null;
  summary: string;
  hours_ago: number;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

const SEEDS: LogSeed[] = [
  {
    event_type: "ExecutionSucceeded",
    entity_index: 0,
    actor_kind: "rule_engine",
    actor_label: "Regla M03",
    proposal_id: "prop_hist_1",
    summary: "Presupuesto diario 172 € → 120 € por CPL sobre umbral",
    hours_ago: 1,
    before: { presupuesto_diario: 172 },
    after: { presupuesto_diario: 120 },
  },
  {
    event_type: "ProposalApproved",
    entity_index: 2,
    actor_kind: "owner",
    actor_label: "Dueño",
    proposal_id: "prop_003",
    summary: "Aprobó subida de presupuesto 197 € → 221 €",
    hours_ago: 3,
    before: { presupuesto_diario: 197 },
    after: { presupuesto_diario: 221 },
  },
  {
    event_type: "RuleFired",
    entity_index: 6,
    actor_kind: "rule_engine",
    actor_label: "Regla X01",
    proposal_id: "prop_002",
    summary: "Disparó propuesta de pausa por 0 conversiones en 21 días",
    hours_ago: 5,
    before: null,
    after: null,
  },
  {
    event_type: "SignalEmitted",
    entity_index: 4,
    actor_kind: "system",
    actor_label: "Ciclo de señales",
    proposal_id: null,
    summary: "Señal SUBIR 45 · limitada por presupuesto con CPL bajo objetivo",
    hours_ago: 8,
    before: null,
    after: null,
  },
  {
    event_type: "ExecutionUndone",
    entity_index: 1,
    actor_kind: "owner",
    actor_label: "Dueño",
    proposal_id: "prop_hist_2",
    summary: "Deshizo el cambio de puja máxima dentro de la ventana de gracia",
    hours_ago: 14,
    before: { puja_maxima: 1.55 },
    after: { puja_maxima: 1.4 },
  },
  {
    event_type: "EmergencyBrakeEngaged",
    entity_index: null,
    actor_kind: "owner",
    actor_label: "Dueño",
    proposal_id: null,
    summary: "Freno activado: revisión manual de la cuenta de Meta",
    hours_ago: 26,
    before: null,
    after: null,
  },
  {
    event_type: "EmergencyBrakeReleased",
    entity_index: null,
    actor_kind: "owner",
    actor_label: "Dueño",
    proposal_id: null,
    summary: "Freno desactivado tras revisar la cuenta",
    hours_ago: 25,
    before: null,
    after: null,
  },
  {
    event_type: "ExecutionFailed",
    entity_index: 3,
    actor_kind: "agent",
    actor_label: "Agente de Anuncios",
    proposal_id: "prop_hist_3",
    summary: "Fallo al aplicar el cambio: la plataforma devolvió un error temporal",
    hours_ago: 30,
    before: { presupuesto_diario: 61 },
    after: null,
  },
  {
    event_type: "AdEntityDrifted",
    entity_index: 5,
    actor_kind: "system",
    actor_label: "Sincronización",
    proposal_id: null,
    summary: "Estado divergente con la plataforma: se marcó para reconciliar",
    hours_ago: 48,
    before: null,
    after: null,
  },
  {
    event_type: "ProposalRejected",
    entity_index: 4,
    actor_kind: "owner",
    actor_label: "Dueño",
    proposal_id: "prop_hist_4",
    summary: "Rechazó la subida de presupuesto: sin margen este mes",
    hours_ago: 72,
    before: null,
    after: null,
  },
];

export function listDecisionLog() {
  const items = SEEDS.map((seed, index) => {
    const entity = seed.entity_index === null ? null : e(seed.entity_index);
    return {
      seq: SEEDS.length - index,
      business_id: "biz_ejemplo",
      event_type: seed.event_type,
      entity_ref: entity?.ref ?? null,
      entity_name: entity?.name ?? null,
      actor_kind: seed.actor_kind,
      actor_label: seed.actor_label,
      proposal_id: seed.proposal_id,
      summary: seed.summary,
      before: seed.before,
      after: seed.after,
      occurred_at: hoursAgo(seed.hours_ago),
    };
  });
  return { items, next_cursor: null };
}

/** Estado de la cadena de hash — cambia a "rota" activando `FORCE_CHAIN_BROKEN` en el fixture si hiciera falta un demo. */
export function verifyDecisionLogChain() {
  return { verified_through_seq: SEEDS.length, chain_ok: true, checked_at: new Date().toISOString() };
}

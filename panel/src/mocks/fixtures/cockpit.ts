import type { CockpitView, Measure, MeasureStatus, TickerRow } from "@/api/schemas/cockpit";
import { series } from "./deterministicRandom";

const EUR = "EUR";
const money = (amount: number) => ({ amount: amount.toFixed(2), currency: EUR });

function available<T>(value: T): Measure<T> {
  return { status: "available", value };
}
function unavailable(status: Exclude<MeasureStatus, "available">, reason: string): Measure<never> {
  return { status, value: null, reason };
}

const DEGRADED_ACCOUNT_IDS_BY_BUSINESS: Record<string, string[]> = {
  biz_norte: ["meta:act_100000000000001"],
};

/**
 * Estado mínimo, sólo de pruebas, para que "Deshacer" sobre una fila `autonomous_applied` deje
 * de ofrecer el botón tras la llamada real — mismo patrón que el `store` de `fixtures/proposals.ts`.
 */
const undoneExecutionIds = new Set<string>();
export function markCockpitExecutionUndone(executionId: string) {
  undoneExecutionIds.add(executionId);
}

/**
 * Pausar/Reanudar/Eliminar desde Campañas (design.md §4.2/§7.2/§7.3) actúan directo sobre la
 * entidad, no sobre una propuesta: este mapa deja el cambio visible la próxima vez que se lee
 * `/cockpit`, igual que `undoneExecutionIds` hace para "Deshacer".
 */
const entityStatusOverrides = new Map<string, "ACTIVE" | "PAUSED" | "REMOVED">();
export function setCockpitEntityStatus(entityRef: string, status: "ACTIVE" | "PAUSED" | "REMOVED") {
  entityStatusOverrides.set(entityRef, status);
}
export function getCockpitEntityStatus(entityRef: string, fallback: string): string {
  return entityStatusOverrides.get(entityRef) ?? fallback;
}

/** Estático por entidad (no depende del negocio) — suficiente para que el mock de pausar/reanudar/eliminar resuelva `is_controllable` y la cuenta dueña sin un `business_id`. */
export function findCockpitRow(entityRef: string): TickerRow | undefined {
  return buildRows([]).find((row) => row.entity_ref === entityRef);
}

export const ENTITY_ACTION_GRACE_SECONDS = 15;

interface EntityActionRecord {
  entityRef: string;
  previousStatus: "ACTIVE" | "PAUSED";
  graceDeadlineMs: number;
  undone: boolean;
}
const entityActionExecutions = new Map<string, EntityActionRecord>();

export function recordCockpitEntityAction(executionId: string, entityRef: string, previousStatus: "ACTIVE" | "PAUSED") {
  entityActionExecutions.set(executionId, { entityRef, previousStatus, graceDeadlineMs: Date.now() + ENTITY_ACTION_GRACE_SECONDS * 1000, undone: false });
}

export function undoCockpitEntityAction(executionId: string): boolean {
  const record = entityActionExecutions.get(executionId);
  if (!record || record.undone || Date.now() > record.graceDeadlineMs) return false;
  entityStatusOverrides.set(record.entityRef, record.previousStatus);
  record.undone = true;
  return true;
}

export function resetCockpitFixtures() {
  undoneExecutionIds.clear();
  entityStatusOverrides.clear();
  entityActionExecutions.clear();
}

/**
 * Siete filas, cada una pensada para ejercitar una combinación distinta de estado de `Measure`
 * y de `RowAction` (T008/T011): nunca dos filas prueban lo mismo dos veces.
 */
function buildRows(degradedAccountIds: string[]): TickerRow[] {
  const rows: TickerRow[] = [
    {
      entity_ref: "google:campaign:c-display-retargeting",
      name: "Display Retargeting",
      level: "campaign",
      platform: "google",
      platform_account_id: "google:100-000-0002",
      status: "ACTIVE",
      signal: { signal_id: "sig_buy_1", kind: "BUY", strength: 81, cause: "Coste por conversión un 22 % bajo el objetivo en 14 días", emitted_at: new Date(Date.now() - 3 * 3_600_000).toISOString() },
      money_at_stake: money(620),
      expected_contribution_delta: available(money(140)),
      roi: available(2.8),
      roas: available(4.1),
      leads: available(38),
      customers: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      customer_value: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      cost_per_lead: { actual: available(money(14.2)), target: available(money(18)), delta_pct: available(-21.1) },
      spend: money(205.1),
      cap: money(220),
      pacing_index_pct: available(103),
      sparkline: { metric: "spend", points: series("c-display-retargeting", 14, 14.6, 0.35) },
      freshness: { lag_minutes: 12, is_stale: false },
      is_controllable: true,
      is_degraded: degradedAccountIds.includes("google:100-000-0002"),
      learning_state: { is_learning: false, reason: null, since: null },
      action: {
        kind: "approve_increase",
        mode: "inline_approval",
        friction: "confirm",
        requires_evidence: true,
        diff_hash: "cockpit-diff-buy-1",
        reason: null,
        proposal_id: "prop_buy_1",
        applied_change: null,
        blocked_reason: null,
        target: { method: "POST", path: "/api/v1/proposals/prop_buy_1/approve" },
      },
    },
    {
      entity_ref: "google:campaign:c-brand-search",
      name: "Búsqueda Marca",
      level: "campaign",
      platform: "google",
      platform_account_id: "google:100-000-0002",
      status: "ACTIVE",
      signal: { signal_id: "sig_sell_1", kind: "SELL", strength: 74, cause: "Coste por lead 34 € frente al umbral de 25 € en 7 días", emitted_at: new Date(Date.now() - 40 * 60_000).toISOString() },
      money_at_stake: money(410),
      expected_contribution_delta: available(money(-58)),
      roi: available(0.9),
      roas: available(1.6),
      leads: available(21),
      customers: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      customer_value: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      cost_per_lead: { actual: available(money(34)), target: available(money(25)), delta_pct: available(36) },
      spend: money(172.4),
      cap: money(180),
      pacing_index_pct: available(96),
      sparkline: { metric: "spend", points: series("c-brand-search", 14, 12.3, 0.35) },
      freshness: { lag_minutes: 12, is_stale: false },
      is_controllable: true,
      is_degraded: degradedAccountIds.includes("google:100-000-0002"),
      learning_state: { is_learning: false, reason: null, since: null },
      action: undoneExecutionIds.has("exec_sell_1")
        ? {
            kind: "review_proposal",
            mode: "proposal",
            friction: "none",
            requires_evidence: false,
            diff_hash: null,
            reason: null,
            proposal_id: "prop_sell_1_reverted",
            applied_change: null,
            blocked_reason: null,
            target: { method: "POST", path: "/api/v1/proposals/prop_sell_1_reverted/approve" },
          }
        : {
            kind: "apply_decrease",
            mode: "autonomous_applied",
            friction: "none",
            requires_evidence: false,
            diff_hash: null,
            reason: null,
            proposal_id: null,
            applied_change: {
              parameter: "daily_budget",
              before: "18,00 €",
              after: "14,00 €",
              applied_at: new Date(Date.now() - 5 * 60_000).toISOString(),
              undo_deadline: new Date(Date.now() + 40 * 60_000).toISOString(),
            },
            blocked_reason: null,
            target: { method: "POST", path: "/api/v1/executions/exec_sell_1/undo" },
          },
    },
    {
      entity_ref: "google:campaign:c-busqueda-competencia",
      name: "Búsqueda Competencia",
      level: "campaign",
      platform: "google",
      platform_account_id: "google:100-000-0002",
      status: "ACTIVE",
      signal: { signal_id: "sig_exit_1", kind: "EXIT", strength: 88, cause: "Sin conversiones en 21 días con gasto sostenido", emitted_at: new Date(Date.now() - 90 * 60_000).toISOString() },
      money_at_stake: money(340),
      expected_contribution_delta: available(money(-96)),
      roi: unavailable("insufficient_volume", "Menos de 30 conversiones en la ventana: base insuficiente para un ROI fiable."),
      roas: unavailable("insufficient_volume", "Menos de 30 conversiones en la ventana: base insuficiente para un ROAS fiable."),
      leads: available(4),
      customers: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      customer_value: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      cost_per_lead: { actual: available(money(30.4)), target: available(money(20)), delta_pct: available(52) },
      spend: money(121.7),
      cap: money(160),
      pacing_index_pct: available(76),
      sparkline: { metric: "spend", points: series("c-busqueda-competencia", 14, 8.7, 0.35) },
      freshness: { lag_minutes: 12, is_stale: false },
      is_controllable: true,
      is_degraded: degradedAccountIds.includes("google:100-000-0002"),
      learning_state: { is_learning: false, reason: null, since: null },
      action: {
        kind: "review_proposal",
        mode: "proposal",
        friction: "none",
        requires_evidence: false,
        diff_hash: null,
        reason: null,
        proposal_id: "prop_exit_1",
        applied_change: null,
        blocked_reason: null,
        target: { method: "POST", path: "/api/v1/proposals/prop_exit_1/approve" },
      },
    },
    {
      entity_ref: "meta:campaign:c-meta-advantage-catalogo",
      name: "Meta Advantage+ Catálogo",
      level: "campaign",
      platform: "meta",
      platform_account_id: "meta:act_100000000000001",
      status: "ACTIVE",
      signal: { signal_id: "sig_buy_2", kind: "BUY", strength: 45, cause: "Coste por lead mejora un 9 % en 7 días", emitted_at: new Date(Date.now() - 6 * 3_600_000).toISOString() },
      money_at_stake: money(90),
      expected_contribution_delta: unavailable("not_controllable", "Presupuesto compartido con otro conjunto de anuncios: sin palanca propia."),
      roi: unavailable("not_controllable", "Presupuesto compartido con otro conjunto de anuncios: sin palanca propia."),
      roas: unavailable("not_controllable", "Presupuesto compartido con otro conjunto de anuncios: sin palanca propia."),
      leads: available(12),
      customers: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      customer_value: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      cost_per_lead: { actual: available(money(16.9)), target: available(money(18)), delta_pct: available(-6.1) },
      spend: money(197.5),
      cap: money(200),
      pacing_index_pct: unavailable("not_controllable", "Presupuesto compartido con otro conjunto de anuncios: sin palanca propia."),
      sparkline: { metric: "spend", points: series("c-meta-advantage-catalogo", 14, 14.1, 0.35) },
      freshness: { lag_minutes: 12, is_stale: false },
      is_controllable: false,
      is_degraded: degradedAccountIds.includes("meta:act_100000000000001"),
      learning_state: { is_learning: false, reason: null, since: null },
      action: {
        kind: "none",
        mode: "blocked",
        friction: "none",
        requires_evidence: false,
        diff_hash: null,
        reason: null,
        proposal_id: null,
        applied_change: null,
        blocked_reason: "not_controllable",
        target: null,
      },
    },
    {
      entity_ref: "meta:campaign:c-meta-lookalike",
      name: "Meta Lookalike Clientes",
      level: "campaign",
      platform: "meta",
      platform_account_id: "meta:act_100000000000001",
      status: "LEARNING",
      signal: null,
      money_at_stake: money(0),
      expected_contribution_delta: unavailable("learning", "Entidad creada hace menos de 7 días: en aprendizaje de la plataforma."),
      roi: unavailable("immature_window", "Madurez de atribución 0,22, por debajo de 0,30: ventana insuficiente para decidir."),
      roas: unavailable("immature_window", "Madurez de atribución 0,22, por debajo de 0,30: ventana insuficiente para decidir."),
      leads: unavailable("insufficient_volume", "Menos de 30 conversiones en la ventana: base insuficiente."),
      customers: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      customer_value: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      cost_per_lead: {
        actual: unavailable("no_data", "Sin conversiones registradas en la ventana."),
        target: available(money(20)),
        delta_pct: unavailable("no_data", "Sin conversiones registradas en la ventana."),
      },
      spend: money(61.3),
      cap: money(90),
      pacing_index_pct: available(68),
      sparkline: { metric: "spend", points: series("c-meta-lookalike", 14, 4.4, 0.35) },
      freshness: { lag_minutes: 12, is_stale: false },
      is_controllable: true,
      is_degraded: degradedAccountIds.includes("meta:act_100000000000001"),
      learning_state: { is_learning: true, reason: "Entidad creada hace menos de 7 días", since: new Date(Date.now() - 3 * 86_400_000).toISOString() },
      action: {
        kind: "none",
        mode: "blocked",
        friction: "none",
        requires_evidence: false,
        diff_hash: null,
        reason: null,
        proposal_id: null,
        applied_change: null,
        blocked_reason: "immature_window",
        target: null,
      },
    },
    {
      entity_ref: "google:campaign:c-busqueda-generica",
      name: "Búsqueda Genérica",
      level: "campaign",
      platform: "google",
      platform_account_id: "google:100-000-0002",
      status: "ACTIVE",
      signal: { signal_id: "sig_sell_2", kind: "SELL", strength: 58, cause: "Coste por lead sobre el umbral en la última semana", emitted_at: new Date(Date.now() - 5 * 3_600_000).toISOString() },
      money_at_stake: money(180),
      expected_contribution_delta: unavailable("stale", "Dato de hace más de 60 min: última lectura conservada, sin escritura."),
      roi: unavailable("stale", "Dato de hace más de 60 min: última lectura conservada, sin escritura."),
      roas: unavailable("stale", "Dato de hace más de 60 min: última lectura conservada, sin escritura."),
      leads: unavailable("stale", "Dato de hace más de 60 min: última lectura conservada, sin escritura."),
      customers: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      customer_value: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      cost_per_lead: {
        actual: unavailable("stale", "Dato de hace más de 60 min: última lectura conservada, sin escritura."),
        target: available(money(22)),
        delta_pct: unavailable("stale", "Dato de hace más de 60 min: última lectura conservada, sin escritura."),
      },
      spend: money(96.2),
      cap: money(130),
      pacing_index_pct: unavailable("stale", "Dato de hace más de 60 min: última lectura conservada, sin escritura."),
      sparkline: { metric: "spend", points: series("c-busqueda-generica", 14, 6.9, 0.35) },
      freshness: { lag_minutes: 87, is_stale: true },
      is_controllable: true,
      is_degraded: degradedAccountIds.includes("google:100-000-0002"),
      learning_state: { is_learning: false, reason: null, since: null },
      action: {
        kind: "apply_decrease",
        mode: "blocked",
        friction: "none",
        requires_evidence: false,
        diff_hash: null,
        reason: null,
        proposal_id: null,
        applied_change: null,
        blocked_reason: "stale_data",
        target: null,
      },
    },
    {
      entity_ref: "meta:campaign:c-meta-prospeccion",
      name: "Meta Prospección",
      level: "campaign",
      platform: "meta",
      platform_account_id: "meta:act_100000000000001",
      status: "ACTIVE",
      signal: { signal_id: "sig_hold_1", kind: "HOLD", strength: 32, cause: "Dentro de rango: sin desviación relevante en 7 días", emitted_at: new Date(Date.now() - 2 * 3_600_000).toISOString() },
      money_at_stake: money(0),
      expected_contribution_delta: available(money(6)),
      roi: available(1.4),
      roas: available(2.2),
      leads: available(17),
      customers: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      customer_value: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      cost_per_lead: { actual: available(money(21.4)), target: available(money(22)), delta_pct: available(-2.7) },
      spend: money(148.9),
      cap: money(150),
      pacing_index_pct: available(99),
      sparkline: { metric: "spend", points: series("c-meta-prospeccion", 14, 10.6, 0.35) },
      freshness: { lag_minutes: 12, is_stale: false },
      is_controllable: true,
      is_degraded: degradedAccountIds.includes("meta:act_100000000000001"),
      learning_state: { is_learning: false, reason: null, since: null },
      action: {
        kind: "none",
        mode: "blocked",
        friction: "none",
        requires_evidence: false,
        diff_hash: null,
        reason: null,
        proposal_id: null,
        applied_change: null,
        blocked_reason: "guardrail",
        target: null,
      },
    },
    {
      entity_ref: "meta:campaign:c-retargeting-frio",
      name: "Retargeting frío",
      level: "campaign",
      platform: "meta",
      platform_account_id: "meta:act_100000000000001",
      status: "PAUSED",
      signal: { signal_id: "sig_exit_2", kind: "EXIT", strength: 61, cause: "Lleva 9 días gastando sin traer ni un lead.", emitted_at: new Date(Date.now() - 9 * 86_400_000).toISOString() },
      money_at_stake: money(0),
      expected_contribution_delta: unavailable("no_data", "Campaña pausada: sin gasto que proyectar."),
      roi: unavailable("no_data", "Campaña pausada: sin gasto que proyectar."),
      roas: unavailable("no_data", "Campaña pausada: sin gasto que proyectar."),
      leads: available(0),
      customers: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      customer_value: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."),
      cost_per_lead: { actual: unavailable("no_data", "Sin conversiones registradas en la ventana."), target: available(money(20)), delta_pct: unavailable("no_data", "Sin conversiones registradas en la ventana.") },
      spend: money(0),
      cap: money(20),
      pacing_index_pct: unavailable("no_data", "Campaña pausada: sin gasto que proyectar."),
      sparkline: { metric: "spend", points: series("c-retargeting-frio", 14, 2.8, 0.35) },
      freshness: { lag_minutes: 12, is_stale: false },
      is_controllable: true,
      is_degraded: degradedAccountIds.includes("meta:act_100000000000001"),
      learning_state: { is_learning: false, reason: null, since: null },
      action: {
        kind: "none",
        mode: "blocked",
        friction: "none",
        requires_evidence: false,
        diff_hash: null,
        reason: null,
        proposal_id: null,
        applied_change: null,
        blocked_reason: null,
        target: null,
      },
    },
  ];

  return rows;
}

export function buildCockpit(businessId: string, window: "today" | "7d" | "30d" = "7d"): CockpitView {
  const degradedAccountIds = DEGRADED_ACCOUNT_IDS_BY_BUSINESS[businessId] ?? [];
  const rows = buildRows(degradedAccountIds)
    .map((row) => ({ ...row, status: getCockpitEntityStatus(row.entity_ref, row.status) }))
    .filter((row) => row.status !== "REMOVED");
  const isPartial = degradedAccountIds.length > 0;

  return {
    business_id: businessId,
    window,
    currency: EUR,
    generated_at: new Date().toISOString(),
    freshness: { last_ingested_at: new Date(Date.now() - 12 * 60_000).toISOString(), lag_minutes: 12, is_stale: false },
    is_partial: isPartial,
    degraded_accounts: isPartial
      ? [{ platform_account_id: "meta:act_100000000000001", platform: "meta", status: "READ_ONLY", reason: "Meta Ads en solo lectura desde hace 1 h" }]
      : [],
    header: {
      spend: { today: money(112.4), mtd: money(2840.6), window: money(786.1) },
      caps: { daily: money(140), monthly: money(3200), source: "guardrail" },
      pacing: { index_pct: 103, projection_pct: 107, days_remaining: 9 },
      projected_month_end: available(money(3120)),
      leads: { today: available(19), week: available(92) },
      customers: { today: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta."), week: unavailable("no_customer_source", "CRM de clientes no conectado: hasta entonces el ROI se declara sobre venta.") },
      roi: available(1.6),
      roi_basis: "revenue",
      roi_basis_reason: "CRM de clientes no conectado: el ROI se declara sobre venta, no sobre cliente.",
      roas: available(2.7),
      cost_per_lead: { actual: available(money(19.4)), target: available(money(20)), delta_pct: available(-3) },
      brake: { engaged: false, mode: null, since: null },
      proposals: { pending: 6, deferred: 3, critical: 1 },
    },
    rows,
    changes_since: {
      since: new Date(Date.now() - 24 * 3_600_000).toISOString(),
      is_partial: isPartial,
      items: [
        {
          kind: "signal_changed",
          entity_ref: "google:campaign:c-display-retargeting",
          entity_name: "Display Retargeting",
          before: "MANTENER",
          after: "SUBIR",
          occurred_at: new Date(Date.now() - 3 * 3_600_000).toISOString(),
          detail_ref: "google:campaign:c-display-retargeting",
        },
        {
          kind: "autonomous_applied",
          entity_ref: "google:campaign:c-brand-search",
          entity_name: "Búsqueda Marca",
          before: "18,00 €",
          after: "14,00 €",
          occurred_at: new Date(Date.now() - 5 * 60_000).toISOString(),
          detail_ref: "google:campaign:c-brand-search",
        },
        {
          kind: "threshold_crossed",
          entity_ref: "google:campaign:c-busqueda-competencia",
          entity_name: "Búsqueda Competencia",
          before: "18 días sin conversión",
          after: "21 días sin conversión",
          occurred_at: new Date(Date.now() - 90 * 60_000).toISOString(),
          detail_ref: "google:campaign:c-busqueda-competencia",
        },
        {
          kind: "entity_entered",
          entity_ref: "meta:campaign:c-meta-lookalike",
          entity_name: "Meta Lookalike Clientes",
          before: null,
          after: "En aprendizaje",
          occurred_at: new Date(Date.now() - 3 * 86_400_000).toISOString(),
          detail_ref: "meta:campaign:c-meta-lookalike",
        },
        {
          kind: "entity_exited",
          entity_ref: "google:campaign:c-black-friday",
          entity_name: "Black Friday Search",
          before: "SUBIR",
          after: null,
          occurred_at: new Date(Date.now() - 20 * 3_600_000).toISOString(),
          detail_ref: null,
        },
      ],
    },
  };
}

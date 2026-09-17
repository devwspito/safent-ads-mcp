/**
 * Payload real de `GET /api/v1/proposals/{id}` para una propuesta Google `create_campaign`
 * (spec 005-google-campaign-types): un plan `SEARCH` con `geographic_targeting`, un campo
 * que `campaignCreationSchema` no conocía y que `.strict()` rechazaba entero — el bug que
 * bloqueaba "Revisar y aprobar" en /propuestas para Google mientras Meta funcionaba.
 * Capturado el 16-sep-2026 contra la instalación real; sólo `expires_at` se recalcula para
 * no caducar con el paso del tiempo.
 */
const CREATION_PLAN = {
  name: "PRUEBA TECNICA Safent - no activar",
  native: {
    bidding_strategy: "MANUAL_CPC",
    network_settings: {
      target_google_search: true,
      target_search_network: false,
      target_content_network: false,
      target_partner_search_network: false,
    },
    geographic_targeting: {
      geo_target_constants: ["geoTargetConstants/2724"],
      positive_geo_target_type: "PRESENCE",
    },
    advertising_channel_type: "SEARCH",
    contains_eu_political_advertising: "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
  },
  status: "PAUSED",
  platform: "google",
  daily_budget: { amount: "5.00", currency: "EUR" },
  schema_version: 1,
};

const VALOR_PROPUESTO = JSON.stringify({
  angle: "PRUEBA TECNICA Safent - no activar",
  calendar_event_id: null,
  creation_plan: CREATION_PLAN,
  daily_budget_amount: "5.00",
  daily_budget_currency: "EUR",
  duration_days: 7,
  geo: "ES",
  kill_criterion: "Prueba tecnica: se borra tras verificar; nunca se activa",
  objective: "traffic",
  offering_id: "5dd8aeea-dbcd-4deb-855b-73b0d11c9cd0",
  platform: "google",
  success_criterion: "Prueba tecnica: la campana existe en la plataforma en estado PAUSED",
  targeting_seed: "Madrid, duenos de perros",
});

/** Tal cual `panel_read.py` lo sirve — usar `proposalDetailSchema.parse(...)` en los tests, nunca sin validar. */
export function googleSearchWithTargetingDetail(): Record<string, unknown> {
  return {
    item_kind: "proposal",
    action_kind: "create_campaign",
    proposal_id: "8b60b22c-73d0-4f03-b918-146df91a3903",
    proposed_by: null,
    entity_ref: "google:account:0609e9cf-e861-4c9b-94cf-4611e527fc69:356dd45b-43fd-442d-8005-0990a235957e:1000000001",
    entity_name: "google:account:0609e9cf-e861-4c9b-94cf-4611e527fc69:356dd45b-43fd-442d-8005-0990a235957e:1000000001",
    platform: "google",
    diff: {
      parametro: "new_campaign:e99bb26c1fae38591d62615267f3879bebfbdc45d857ccdb",
      valor_actual: "null",
      valor_propuesto: VALOR_PROPUESTO,
      diff_hash: "58c989a0a5b52ecc38acba37182ecea7fade6b22d6d62e83022ad3e4d1528e03",
      currency: null,
    },
    classification: "important",
    urgency: "minor",
    risk_level: "medium",
    requires_expansion: true,
    requires_typed_confirmation: false,
    estimated_impact: { amount: 35.0, currency: "EUR" },
    cause: "traffic",
    expires_at: new Date(Date.now() + 3 * 86_400_000).toISOString(),
    postponed_until: null,
    // Real capture was already `rejected` (a spent test run); `pending` here so the fixture
    // can exercise the approve path this bug blocked, not just the parse.
    state: "pending",
    creation_plan: CREATION_PLAN,
    creation_plan_error: null,
    cause_key: "opportunity_candidate:opportunity_cycle:google:account:0609e9cf-e861-4c9b-94cf-4611e527fc69:356dd45b-43fd-442d-8005-0990a235957e:1000000001",
    execution_id: null,
    signal_id: null,
    signal: null,
    rule_id: null,
    rule_code: null,
    rule_hit_rate_pct: null,
    rule_hit_rate_sample: 0,
    data_window: "No disponible",
    data_age_minutes: null,
    estimated_impact_range: null,
    evidence: [],
    guardrail_verdicts: [],
    entity_history: [],
    owner_context: null,
    platform_url: null,
  };
}

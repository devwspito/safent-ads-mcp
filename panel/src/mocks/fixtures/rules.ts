/** Catálogo M/G/X del `rule-catalog` — fixture mutable (conmutar apagada/avisar/automática). */
interface RuleRecord {
  rule_id: string;
  code: string;
  name: string;
  scope: "business" | "platform_account" | "campaign";
  platform: "google" | "meta" | null;
  condition_label: string;
  window: string;
  action_label: string;
  magnitude_pct: number;
  autonomy_level: "NOTIFY" | "AUTO" | "APPROVAL";
  cooldown_hours: number;
  is_enabled: boolean;
  firings_30d: number;
  hit_rate_pct: number | null;
  increases_spend: boolean;
}

const rules: RuleRecord[] = [
  {
    rule_id: "rule_M03",
    code: "M03",
    name: "Bajar presupuesto por CPL sobre umbral",
    scope: "campaign",
    platform: null,
    condition_label: "Coste por lead > umbral de la campaña",
    window: "7D",
    action_label: "Bajar presupuesto",
    magnitude_pct: 30,
    autonomy_level: "AUTO",
    cooldown_hours: 24,
    is_enabled: true,
    firings_30d: 6,
    hit_rate_pct: 78.4,
    increases_spend: false,
  },
  {
    rule_id: "rule_X01",
    code: "X01",
    name: "Salir sin conversiones sostenido",
    scope: "campaign",
    platform: null,
    condition_label: "0 conversiones con gasto sostenido 21 días",
    window: "21D",
    action_label: "Pausar entidad",
    magnitude_pct: 100,
    autonomy_level: "APPROVAL",
    cooldown_hours: 72,
    is_enabled: true,
    firings_30d: 2,
    hit_rate_pct: 91.2,
    increases_spend: false,
  },
  {
    rule_id: "rule_G01",
    code: "G01",
    name: "Subir presupuesto limitado con CPL bajo",
    scope: "campaign",
    platform: null,
    condition_label: "IS perdida por presupuesto y CPL bajo objetivo",
    window: "7D",
    action_label: "Subir presupuesto",
    magnitude_pct: 12,
    autonomy_level: "APPROVAL",
    cooldown_hours: 24,
    is_enabled: true,
    firings_30d: 9,
    hit_rate_pct: 84.0,
    increases_spend: true,
  },
  {
    rule_id: "rule_G04",
    code: "G04",
    name: "Ajustar puja por CPL estable",
    scope: "campaign",
    platform: null,
    condition_label: "CPL estable con margen de puja",
    window: "14D",
    action_label: "Subir puja máxima",
    magnitude_pct: 10,
    autonomy_level: "NOTIFY",
    cooldown_hours: 48,
    is_enabled: false,
    firings_30d: 0,
    hit_rate_pct: 66.7,
    increases_spend: true,
  },
  {
    rule_id: "rule_M05",
    code: "M05",
    name: "Bajar presupuesto por ROAS bajo",
    scope: "platform_account",
    platform: "meta",
    condition_label: "ROAS bajo objetivo en 3D y 7D",
    window: "3D/7D",
    action_label: "Bajar presupuesto",
    magnitude_pct: 30,
    autonomy_level: "AUTO",
    cooldown_hours: 24,
    is_enabled: true,
    firings_30d: 4,
    hit_rate_pct: 88.1,
    increases_spend: false,
  },
];

export function listRules() {
  return { items: rules };
}

export type UpdateRuleResult =
  | { ok: true; rule: RuleRecord }
  | { ok: false; error: "AUTO_INCREASES_SPEND" }
  | { ok: false; error: "AUTONOMY_GATE_OPEN"; missing: GateQuestion[] };

export function updateRule(
  ruleId: string,
  patch: Partial<Pick<RuleRecord, "is_enabled" | "autonomy_level" | "magnitude_pct">>,
): UpdateRuleResult | null {
  const rule = rules.find((r) => r.rule_id === ruleId);
  if (!rule) return null;
  if (patch.autonomy_level === "AUTO") {
    if (rule.increases_spend) return { ok: false, error: "AUTO_INCREASES_SPEND" };
    const gate = autonomyGate();
    if (!gate.ready) {
      const blocking = gate.accounts.find((account) => !account.ready);
      return { ok: false, error: "AUTONOMY_GATE_OPEN", missing: blocking?.missing ?? [] };
    }
  }
  Object.assign(rule, patch);
  return { ok: true, rule };
}

interface GuardrailRecord {
  guardrail_id: string;
  scope: "business" | "platform_account" | "campaign";
  scope_label: string;
  daily_cap: number;
  monthly_cap: number;
  budget_floor: number;
  budget_ceiling: number;
  max_step_pct: number;
  max_changes_per_entity_per_day: number;
  min_viable_spend: number;
  currency: string;
}

const guardrails: GuardrailRecord[] = [
  {
    guardrail_id: "gr_business",
    scope: "business",
    scope_label: "Negocio Ejemplo (global)",
    daily_cap: 900,
    monthly_cap: 22_000,
    budget_floor: 20,
    budget_ceiling: 600,
    max_step_pct: 20,
    max_changes_per_entity_per_day: 2,
    min_viable_spend: 30,
    currency: "EUR",
  },
  {
    guardrail_id: "gr_google",
    scope: "platform_account",
    scope_label: "Google Ads — Negocio Ejemplo",
    daily_cap: 500,
    monthly_cap: 12_000,
    budget_floor: 15,
    budget_ceiling: 400,
    max_step_pct: 20,
    max_changes_per_entity_per_day: 2,
    min_viable_spend: 25,
    currency: "EUR",
  },
  {
    guardrail_id: "gr_meta",
    scope: "platform_account",
    scope_label: "Meta Ads — Negocio Ejemplo",
    daily_cap: 400,
    monthly_cap: 10_000,
    budget_floor: 15,
    budget_ceiling: 350,
    max_step_pct: 20,
    max_changes_per_entity_per_day: 2,
    min_viable_spend: 25,
    currency: "EUR",
  },
];

export function listGuardrails() {
  return { items: guardrails };
}

export function updateGuardrail(guardrailId: string, update: Omit<GuardrailRecord, "guardrail_id" | "scope" | "scope_label" | "currency">) {
  const guardrail = guardrails.find((g) => g.guardrail_id === guardrailId);
  if (!guardrail) return null;
  Object.assign(guardrail, update);
  return guardrail;
}

type GateQuestionKey = "q2_autonomous_decrease" | "q3_monthly_cap" | "q8_browser_path";

interface GateQuestion {
  key: GateQuestionKey;
  label: string;
  recommended_default: string | null;
}

const GATE_QUESTIONS: GateQuestion[] = [
  { key: "q2_autonomous_decrease", label: "Pregunta 2: ¿bajadas automáticas de presupuesto?", recommended_default: "sí, hasta -20 %" },
  { key: "q3_monthly_cap", label: "Pregunta 3: tope mensual duro", recommended_default: null },
  { key: "q8_browser_path", label: "Pregunta 8: ruta de navegador para cambios manuales", recommended_default: null },
];

interface GateConfirmation {
  key: GateQuestionKey;
  value: string;
  confirmed_at: string;
  confirmed_by: string;
}

interface GateAccountRecord {
  platform_account_id: string;
  label: string;
  confirmed: GateConfirmation[];
}

/** T120: preguntas 2, 3 y 8 de spec.md, confirmadas POR CUENTA (rest-api.md §Reglas). */
function defaultGateAccounts(): GateAccountRecord[] {
  return [
    { platform_account_id: "google:100-000-0002", label: "Google Ads — Negocio Ejemplo", confirmed: [] },
    {
      platform_account_id: "meta:act_100000000000001",
      label: "Meta Ads — Negocio Ejemplo",
      confirmed: GATE_QUESTIONS.map((q) => ({
        key: q.key,
        value: q.recommended_default ?? "confirmado",
        confirmed_at: new Date(Date.now() - 2 * 86_400_000).toISOString(),
        confirmed_by: "owner_demo",
      })),
    },
  ];
}

let gateAccounts: GateAccountRecord[] = defaultGateAccounts();

/** Restaura las confirmaciones de la puerta de autonomía — usar entre tests que confirman preguntas. */
export function resetRulesFixtures() {
  gateAccounts = defaultGateAccounts();
}

export function autonomyGate() {
  const accounts = gateAccounts.map((account) => {
    const missing = GATE_QUESTIONS.filter((q) => !account.confirmed.some((c) => c.key === q.key));
    return { platform_account_id: account.platform_account_id, label: account.label, ready: missing.length === 0, missing, confirmed: account.confirmed };
  });
  return { ready: accounts.every((account) => account.ready), accounts };
}

export function confirmAutonomyGateQuestion(input: { platform_account_id: string; key: GateQuestionKey; value: string }): boolean {
  const account = gateAccounts.find((a) => a.platform_account_id === input.platform_account_id);
  if (!account) return false;
  account.confirmed = [
    ...account.confirmed.filter((c) => c.key !== input.key),
    { key: input.key, value: input.value, confirmed_at: new Date().toISOString(), confirmed_by: "owner_demo" },
  ];
  return true;
}

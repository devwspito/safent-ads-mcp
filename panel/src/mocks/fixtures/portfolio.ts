import type { PortfolioResponse, PortfolioRow } from "@/api/schemas";
import { series } from "./deterministicRandom";

const EUR = "EUR";

interface CampaignSeed {
  entity_ref: string;
  name: string;
  platform: "google" | "meta";
  platform_account_id: string;
  status: PortfolioRow["status"];
  budget: number;
  spend: number;
  cost_per_lead: number | null;
  signal: PortfolioRow["signal"];
  money_at_stake: number;
  is_controllable: boolean;
  learning: boolean;
}

const DEMO_CAMPAIGNS: CampaignSeed[] = [
  {
    entity_ref: "google:campaign:c-brand-search",
    name: "Búsqueda Marca",
    platform: "google",
    platform_account_id: "google:100-000-0002",
    status: "ACTIVE",
    budget: 180,
    spend: 172.4,
    cost_per_lead: 18.6,
    signal: { kind: "SELL", strength: 74, cause: "Coste por lead 34 € frente al umbral de 25 € en 7 días" },
    money_at_stake: 410,
    is_controllable: true,
    learning: false,
  },
  {
    entity_ref: "google:campaign:c-display-retargeting",
    name: "Display Retargeting",
    platform: "google",
    platform_account_id: "google:100-000-0002",
    status: "ACTIVE",
    budget: 220,
    spend: 205.1,
    cost_per_lead: 14.2,
    signal: { kind: "BUY", strength: 81, cause: "Coste por conversión un 22 % bajo el objetivo en 14 días" },
    money_at_stake: 620,
    is_controllable: true,
    learning: false,
  },
  {
    entity_ref: "meta:campaign:c-meta-prospeccion",
    name: "Meta Prospección",
    platform: "meta",
    platform_account_id: "meta:act_100000000000001",
    status: "ACTIVE",
    budget: 150,
    spend: 148.9,
    cost_per_lead: 21.4,
    signal: { kind: "HOLD", strength: 32, cause: "Dentro de rango: sin desviación relevante en 7 días" },
    money_at_stake: 0,
    is_controllable: true,
    learning: false,
  },
  {
    entity_ref: "meta:campaign:c-meta-lookalike",
    name: "Meta Lookalike Clientes",
    platform: "meta",
    platform_account_id: "meta:act_100000000000001",
    status: "LEARNING",
    budget: 90,
    spend: 61.3,
    cost_per_lead: null,
    signal: null,
    money_at_stake: 0,
    is_controllable: true,
    learning: true,
  },
  {
    entity_ref: "google:campaign:c-busqueda-generica",
    name: "Búsqueda Genérica",
    platform: "google",
    platform_account_id: "google:100-000-0002",
    status: "ACTIVE",
    budget: 130,
    spend: 96.2,
    cost_per_lead: 26.8,
    signal: { kind: "SELL", strength: 58, cause: "Coste por lead sobre el umbral en la última semana" },
    money_at_stake: 180,
    is_controllable: true,
    learning: false,
  },
  {
    entity_ref: "meta:campaign:c-meta-advantage-catalogo",
    name: "Meta Advantage+ Catálogo",
    platform: "meta",
    platform_account_id: "meta:act_100000000000001",
    status: "ACTIVE",
    budget: 200,
    spend: 197.5,
    cost_per_lead: 16.9,
    signal: { kind: "BUY", strength: 45, cause: "Coste por lead mejora un 9 % en 7 días" },
    money_at_stake: 90,
    is_controllable: false,
    learning: false,
  },
  {
    entity_ref: "google:campaign:c-busqueda-competencia",
    name: "Búsqueda Competencia",
    platform: "google",
    platform_account_id: "google:100-000-0002",
    status: "ACTIVE",
    budget: 160,
    spend: 121.7,
    cost_per_lead: 19.9,
    signal: { kind: "EXIT", strength: 88, cause: "Sin conversiones en 21 días con gasto sostenido" },
    money_at_stake: 340,
    is_controllable: true,
    learning: false,
  },
];

const DEGRADED_ACCOUNT_IDS_BY_BUSINESS: Record<string, string[]> = {
  biz_norte: ["meta:act_100000000000001"],
};

function toRow(seed: CampaignSeed, degradedAccountIds: string[]): PortfolioRow {
  return {
    entity_ref: seed.entity_ref,
    name: seed.name,
    platform: seed.platform,
    platform_account_id: seed.platform_account_id,
    platform_account_uuid: seed.platform_account_id,
    status: seed.status,
    currency: EUR,
    budget: { amount: seed.budget, currency: EUR },
    spend: { amount: seed.spend, currency: EUR },
    cost_per_lead: seed.cost_per_lead === null ? null : { amount: seed.cost_per_lead, currency: EUR },
    signal: seed.signal,
    money_at_stake: { amount: seed.money_at_stake, currency: EUR },
    is_controllable: seed.is_controllable,
    learning_state: {
      is_learning: seed.learning,
      reason: seed.learning ? "Entidad creada hace menos de 7 días" : null,
      since: seed.learning ? new Date(Date.now() - 3 * 86_400_000).toISOString() : null,
    },
    spend_14d: series(seed.entity_ref, 14, seed.spend / 14 || 1, 0.35),
    is_degraded: degradedAccountIds.includes(seed.platform_account_id),
  };
}

export function buildPortfolio(businessId: string, window: string = "7D"): PortfolioResponse {
  const degradedAccountIds = DEGRADED_ACCOUNT_IDS_BY_BUSINESS[businessId] ?? [];
  const rows = DEMO_CAMPAIGNS.map((seed) => toRow(seed, degradedAccountIds));
  const totalSpend = rows.reduce((sum, row) => sum + row.spend.amount, 0);
  const totalBudget = rows.reduce((sum, row) => sum + row.budget.amount, 0);
  const pacingIndex = businessId === "biz_norte" ? 118 : 103;

  return {
    window,
    currency: EUR,
    spend: {
      window: { amount: Math.round(totalSpend * 100) / 100, currency: EUR },
      today: { amount: Math.round((totalSpend / 7) * 100) / 100, currency: EUR },
      mtd: { amount: Math.round(totalSpend * 12.4 * 100) / 100, currency: EUR },
    },
    caps: {
      daily: { amount: Math.round((totalBudget / 7) * 100) / 100, currency: EUR },
      monthly: { amount: Math.round(totalBudget * 22) / 1, currency: EUR },
      source: "guardrail",
    },
    pacing: {
      index_pct: pacingIndex,
      projection_pct: pacingIndex >= 100 ? pacingIndex + 4 : pacingIndex - 3,
      days_remaining: 9,
    },
    conversions_by_kind: { lead: 214, whatsapp: 96, call: 41, business_conversion: 18 },
    cost_per_lead: { amount: 19.4, currency: EUR },
    cost_per_business_conversion: { amount: 231.5, currency: EUR },
    pending_proposals: 6,
    deferred_proposals: 3,
    freshness: { last_ingested_at: new Date(Date.now() - 12 * 60_000).toISOString(), lag_minutes: 12, is_stale: false, no_data: false },
    deviation_vs_platform_pct: -0.4,
    is_partial: degradedAccountIds.length > 0,
    degraded_accounts:
      degradedAccountIds.length > 0
        ? [{ platform_account_id: "meta:act_100000000000001", platform: "meta", status: "READ_ONLY", reason: "Meta Ads en solo lectura desde hace 1 h" }]
        : [],
    rows,
  };
}

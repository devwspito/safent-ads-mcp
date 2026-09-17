import type { PortfolioWindow } from "@/api/queries/portfolio";
import type { Money, PortfolioRow } from "@/api/schemas";
import type { PlatformAccount } from "@/api/schemas/connections";
import { aggregateSeries, seriesTrend, type SeriesTrend } from "@/utils/trend";

export interface AccountResultRow {
  account: PlatformAccount;
  spend: Money;
  /**
   * Reconstruido de `spend / cost_per_lead` — `/portfolio` no trae un recuento de leads por fila
   * todavía (sí un agregado de negocio en `conversions_by_kind.lead`), pero `cost_per_lead` YA es
   * `spend ÷ leads` calculado por el servidor: dividir de vuelta no es una suposición, es la
   * misma cuenta al revés. `null` solo cuando ninguna campaña de la cuenta tiene coste por lead.
   */
  leads: number | null;
  costPerLead: Money | null;
  /** % del presupuesto diario sumado de sus campañas, sobre el gasto real del periodo. `null` sin presupuesto. */
  budgetSharePct: number | null;
  trend: SeriesTrend;
}

function reconstructLeads(row: PortfolioRow): number | null {
  if (!row.cost_per_lead || row.cost_per_lead.amount <= 0) return null;
  return Math.round(row.spend.amount / row.cost_per_lead.amount);
}

export function buildAccountResultRows(
  rows: PortfolioRow[],
  accounts: PlatformAccount[],
  windowDays: number,
  window: PortfolioWindow,
): AccountResultRow[] {
  const result: AccountResultRow[] = [];
  for (const account of accounts) {
    const accountRows = rows.filter((row) => row.platform_account_id === account.platform_account_id);
    if (accountRows.length === 0) continue;

    const currency = accountRows[0]!.currency;
    const spendAmount = accountRows.reduce((sum, row) => sum + row.spend.amount, 0);
    const budgetAmount = accountRows.reduce((sum, row) => sum + row.budget.amount, 0);

    const leadsPerRow = accountRows.map(reconstructLeads);
    const leads = leadsPerRow.every((value) => value === null) ? null : leadsPerRow.reduce((sum: number, value) => sum + (value ?? 0), 0);
    const costPerLead = leads && leads > 0 ? { amount: Math.round((spendAmount / leads) * 100) / 100, currency } : null;
    const budgetSharePct = budgetAmount > 0 ? (spendAmount / (budgetAmount * windowDays)) * 100 : null;
    const trend = seriesTrend(aggregateSeries(accountRows.map((row) => row.spend_14d)), window);

    result.push({ account, spend: { amount: Math.round(spendAmount * 100) / 100, currency }, leads, costPerLead, budgetSharePct, trend });
  }
  return result;
}

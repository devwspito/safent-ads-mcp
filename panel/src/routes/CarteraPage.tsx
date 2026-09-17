import { Link, useSearchParams } from "react-router-dom";
import { useMe } from "@/api/queries/auth";
import { usePlatformAccounts } from "@/api/queries/connections";
import { usePortfolio, type PortfolioWindow } from "@/api/queries/portfolio";
import type { Money, PortfolioResponse } from "@/api/schemas";
import type { PlatformAccount } from "@/api/schemas/connections";
import { KpiTile } from "@/components/kpi/KpiTile";
import { PageHeader } from "@/components/layout/PageHeader";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { ReloadingIndicator } from "@/components/states/ReloadingIndicator";
import { StaleBanner } from "@/components/states/StaleBanner";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { platformAccountDisplayName, platformLabel } from "@/utils/platform";
import { formatMoney, projectedMonthEndSpend } from "@/utils/money";
import { aggregateSeries, seriesTrend } from "@/utils/trend";
import { buildAccountResultRows, type AccountResultRow } from "@/utils/resultsByAccount";
import styles from "./CarteraPage.module.css";

const WINDOWS: PortfolioWindow[] = ["7D", "14D", "30D"];
const WINDOW_LABEL: Record<PortfolioWindow, string> = { "7D": "7 días", "14D": "14 días", "30D": "30 días" };
const WINDOW_DAYS: Record<PortfolioWindow, number> = { "7D": 7, "14D": 14, "30D": 30 };

/** «Resultados» — design.md §5: cómo va, sin decidir nada aquí. Fusiona Cartera y Cockpit. */
export function CarteraPage() {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  return <BusinessResultadosPage key={businessId} businessId={businessId} />;
}

function BusinessResultadosPage({ businessId }: { businessId: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const windowParam = (searchParams.get("window") as PortfolioWindow | null) ?? "7D";
  const platformFilter = searchParams.get("platform");

  const portfolio = usePortfolio(businessId, windowParam);
  const accounts = usePlatformAccounts(businessId);

  function setWindow(next: PortfolioWindow) {
    setSearchParams((prev) => {
      const params = new URLSearchParams(prev);
      params.set("window", next);
      return params;
    });
  }

  function setPlatform(next: string) {
    setSearchParams((prev) => {
      const params = new URLSearchParams(prev);
      if (next) params.set("platform", next);
      else params.delete("platform");
      return params;
    });
  }

  const isLoading = portfolio.isLoading || accounts.isLoading;
  const isError = portfolio.isError || accounts.isError;
  const combined = portfolio.data && accounts.data ? { portfolio: portfolio.data, accounts: accounts.data.items } : undefined;
  const noAccounts = (accounts.data?.items.length ?? 0) === 0;

  return (
    <div>
      <PageHeader
        title="Resultados"
        actions={
          <div className={styles.toolbar}>
            <label className="visually-hidden" htmlFor="resultados-platform-filter">
              Filtrar por plataforma
            </label>
            <select id="resultados-platform-filter" className={styles.select} value={platformFilter ?? ""} onChange={(e) => setPlatform(e.target.value)}>
              <option value="">Todas</option>
              <option value="google">Google</option>
              <option value="meta">Meta</option>
            </select>
            <div className={styles.segmented} role="group" aria-label="Periodo">
              {WINDOWS.map((w) => (
                <button
                  key={w}
                  type="button"
                  className={`${styles.segmentedButton} ${windowParam === w ? styles.segmentedButtonActive : ""}`}
                  aria-pressed={windowParam === w}
                  onClick={() => setWindow(w)}
                >
                  {WINDOW_LABEL[w]}
                </button>
              ))}
            </div>
          </div>
        }
      />
      <p className={styles.context}>
        Últimos {WINDOW_DAYS[windowParam]} días · {combined?.accounts.length ?? 0} {(combined?.accounts.length ?? 0) === 1 ? "cuenta" : "cuentas"}
      </p>

      <QueryBoundary
        isLoading={isLoading}
        isError={isError}
        error={portfolio.error ?? accounts.error}
        onRetry={() => {
          void portfolio.refetch();
          void accounts.refetch();
        }}
        data={combined}
        isEmpty={() => noAccounts}
        emptyTitle="Todavía no hay resultados que mostrar"
        emptyBody="No hay ninguna cuenta conectada."
        emptyAction={<Link to="/ajustes">Conectar una cuenta</Link>}
      >
        {(data) => (
          <ResultadosContent
            data={data.portfolio}
            accounts={data.accounts}
            window={windowParam}
            platformFilter={platformFilter}
            isFetching={portfolio.isFetching && !portfolio.isLoading}
          />
        )}
      </QueryBoundary>
    </div>
  );
}

interface ResultadosContentProps {
  data: PortfolioResponse;
  accounts: PlatformAccount[];
  window: PortfolioWindow;
  platformFilter: string | null;
  isFetching: boolean;
}

function ResultadosContent({ data, accounts, window, platformFilter, isFetching }: ResultadosContentProps) {
  const rows = platformFilter ? data.rows.filter((row) => row.platform === platformFilter) : data.rows;
  const scopedAccounts = platformFilter ? accounts.filter((a) => a.platform === platformFilter) : accounts;

  if (rows.length === 0) {
    return (
      <p className={styles.context}>
        {data.spend.window.amount === 0
          ? `Sin gasto en los últimos ${WINDOW_DAYS[window]} días.`
          : "Todavía no hay resultados que mostrar."}
      </p>
    );
  }

  const degradedIds = new Set(data.degraded_accounts.map((a) => a.platform_account_id));
  const missingNote = data.is_partial
    ? `Faltan datos de ${data.degraded_accounts.map((a) => platformLabel(a.platform)).join(", ")}`
    : null;

  const spendSeries = aggregateSeries(rows.map((row) => row.spend_14d));
  const spendTrend = seriesTrend(spendSeries, window);
  const monthlyCapPhrase = data.caps.monthly ? `de ${formatMoney(data.caps.monthly)} al mes` : "Sin tope";
  const projected = projectedMonthEndSpend(data.spend.mtd);

  const accountRows = buildAccountResultRows(rows, scopedAccounts, WINDOW_DAYS[window], window);
  const byPlatform = groupByPlatform(accountRows);

  return (
    <>
      {isFetching ? <ReloadingIndicator /> : null}
      {data.freshness.is_stale ? <StaleBanner lagMinutes={data.freshness.lag_minutes} /> : null}

      <div className={styles.headerGrid}>
        <KpiTile
          label="Gasto"
          value={formatMoney(data.spend.window)}
          context={monthlyCapPhrase}
          sparkline={spendTrend.sparkline}
          delta={
            spendTrend.deltaPct === null
              ? { text: "Sin comparación disponible a 30 días", valence: "neutral" }
              : { text: `${signedPercent(spendTrend.deltaPct)} vs. los ${WINDOW_DAYS[window] === 14 ? 7 : WINDOW_DAYS[window]} días anteriores`, valence: spendTrend.deltaPct > 0 ? "bad" : "good" }
          }
        />
        <KpiTile label="Leads" value={String(data.conversions_by_kind.lead)} />
        <KpiTile label="Coste por lead" value={data.cost_per_lead ? formatMoney(data.cost_per_lead) : "—"} />
        <KpiTile label="Ritmo del mes" value={formatMoney(projected)} context={ritmoCaption(data.caps.monthly)} />
      </div>
      {missingNote ? <p className={styles.missingNote}>{missingNote}</p> : null}

      <h2 className={styles.sectionTitle}>Por cuenta</h2>
      {byPlatform.map(({ platform, rows: platformAccountRows }) => (
        <div key={platform}>
          <h3 className={styles.platformHeading}>{platformLabel(platform)}</h3>
          {platformAccountRows.map((row) => (
            <AccountResultLine key={row.account.platform_account_id} row={row} degraded={degradedIds.has(row.account.platform_account_id)} />
          ))}
        </div>
      ))}
    </>
  );
}

function AccountResultLine({ row, degraded }: { row: AccountResultRow; degraded: boolean }) {
  const name = platformAccountDisplayName(row.account);
  if (degraded) {
    return (
      <div className={styles.accountRow}>
        <span className={styles.accountName}>{name}</span>
        <span className={styles.missingSpan}>Faltan datos de esta cuenta</span>
      </div>
    );
  }
  return (
    <div className={styles.accountRow}>
      <span className={styles.accountName}>{name}</span>
      <span className={styles.numeric}>{formatMoney(row.spend)} gasto</span>
      <span className={styles.numeric}>{row.leads !== null ? `${row.leads} leads` : "Faltan datos"}</span>
      <span className={styles.numeric}>{row.costPerLead ? `${formatMoney(row.costPerLead)}/lead` : "—"}</span>
      <span className={styles.numeric}>{row.budgetSharePct !== null ? `${sharePercent(row.budgetSharePct)} del tope` : "Sin tope"}</span>
    </div>
  );
}

function groupByPlatform(rows: AccountResultRow[]): Array<{ platform: "google" | "meta"; rows: AccountResultRow[] }> {
  const groups: Array<{ platform: "google" | "meta"; rows: AccountResultRow[] }> = [];
  for (const row of rows) {
    let bucket = groups.find((g) => g.platform === row.account.platform);
    if (!bucket) {
      bucket = { platform: row.account.platform, rows: [] };
      groups.push(bucket);
    }
    bucket.rows.push(row);
  }
  return groups;
}

/** Leyenda de la ficha «Ritmo del mes»: la proyección lleva siempre su tope de referencia, nunca solo el número pelado. */
function ritmoCaption(monthlyCap: Money | null): string {
  return monthlyCap ? `Previsto este mes · tope ${formatMoney(monthlyCap)}` : "Previsto este mes · sin tope";
}

const percentFormatter = new Intl.NumberFormat("es-ES", { minimumFractionDigits: 0, maximumFractionDigits: 1 });

function sharePercent(value: number): string {
  return `${percentFormatter.format(value)} %`;
}

function signedPercent(value: number): string {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${percentFormatter.format(Math.abs(value))} %`;
}

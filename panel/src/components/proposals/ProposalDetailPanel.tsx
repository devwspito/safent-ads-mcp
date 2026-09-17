import { useMe } from "@/api/queries/auth";
import { usePlatformAccounts } from "@/api/queries/connections";
import { useProposalDetail } from "@/api/queries/proposals";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { Sparkline } from "@/components/kpi/Sparkline";
import { campaignCreationSchema, type CampaignCreationPlan } from "@/api/schemas/campaignCreation";
import type { ProposalDetail } from "@/api/schemas/proposals";
import { formatMoney, formatSignedMoney, monthlyEquivalent } from "@/utils/money";
import { accountDisplayName, platformAccountDisplayName, platformLabel } from "@/utils/platform";
import { campaignObjectiveLabel, creationDiffPayload, googleBiddingStrategyLabel, googleChannelTypeLabel, googleConversionGoalsLabel, googleGeographicTargetingLabel } from "@/utils/proposals";
import { isSafeUrl } from "@/utils/url";
import { formatExactDate, formatRelativeTime } from "@/utils/time";
import { PackageLinkChip } from "@/components/packages/PackageLinkChip";
import styles from "./ProposalDetailPanel.module.css";
import { ExecutionStatus } from "./ExecutionStatus";
import { CampaignCreationPanel } from "./CampaignCreationPanel";

/**
 * Compacto y sin ruido para una creación (design.md §2.3, hotfix companion 0.2.20): nombre,
 * presupuesto y su equivalente mensual, dónde se publicará, objetivo y el destino guardado —
 * en vez de los bloques de evidencia/regla/guardarraíles, que no dicen nada de una campaña que
 * todavía no existe.
 */
function CreationSummary({ data }: { data: ProposalDetail }) {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  const accounts = usePlatformAccounts(businessId);
  const plan = campaignCreationSchema.safeParse(data.creation_plan);
  if (!plan.success) return null;

  const { landingUrl } = creationDiffPayload(data.diff);
  const matchedAccount = accounts.data?.items.find((account) => account.platform_account_id === data.account_label);
  const accountName = matchedAccount ? platformAccountDisplayName(matchedAccount) : accountDisplayName(data.platform, data.account_label);
  const dailyBudget = { amount: Number(plan.data.daily_budget.amount), currency: plan.data.daily_budget.currency };
  const geoLabel = plan.data.platform === "google" ? googleGeographicTargetingLabel(plan.data.native) : null;
  const goalsLabel = plan.data.platform === "google" ? googleConversionGoalsLabel(plan.data.native) : null;

  return (
    <div className={styles.block}>
      <span className={styles.blockLabel}>Qué se crea</span>
      <dl className={styles.creationSummary}>
        <div><dt>Nombre</dt><dd>{plan.data.name}</dd></div>
        <div><dt>Presupuesto</dt><dd>{formatSignedMoney(dailyBudget)}/día · {formatMoney(monthlyEquivalent(dailyBudget))}/mes</dd></div>
        <div><dt>Dónde</dt><dd>{platformLabel(plan.data.platform)}{accountName ? ` · ${accountName}` : ""}</dd></div>
        <div><dt>Objetivo</dt><dd>{objectiveDescription(plan.data)}</dd></div>
        {geoLabel ? <div><dt>Segmentación geográfica</dt><dd>{geoLabel}</dd></div> : null}
        {goalsLabel ? <div><dt>Conversiones objetivo</dt><dd>{goalsLabel}</dd></div> : null}
        <div><dt>Estado</dt><dd>En pausa hasta que la actives</dd></div>
        {landingUrl && isSafeUrl(landingUrl) ? <div><dt>Destino</dt><dd>{landingUrl}</dd></div> : null}
      </dl>
    </div>
  );
}

function objectiveDescription(plan: CampaignCreationPlan): string {
  return plan.platform === "google" ? `${googleChannelTypeLabel(plan.native.advertising_channel_type)} · ${googleBiddingStrategyLabel(plan.native)}` : campaignObjectiveLabel(plan.native.objective);
}

interface ProposalDetailPanelProps {
  proposalId: string;
}

/**
 * Nivel 2 — expansión en el sitio, nunca modal: evidencia, regla y su acierto, guardarraíles,
 * historial, impacto como rango, contexto del propietario, enlace a la plataforma.
 * panel-interaction-spec.md §4.
 */
export function ProposalDetailPanel({ proposalId }: ProposalDetailPanelProps) {
  const { data, isLoading, isError, isFetching, refetch } = useProposalDetail(proposalId);

  if (isError) return <div role="alert" className={styles.loading}>No se pudo cargar la evidencia. <button type="button" onClick={() => void refetch()}>Reintentar</button></div>;
  if (isLoading || !data) {
    return <p className={styles.loading}>Cargando evidencia…</p>;
  }

  const isCreation = data.action_kind === "create_campaign";

  return (
    <div className={styles.wrap}>
      {isCreation && <CampaignCreationPanel detail={data} stale={isFetching} onReload={() => void refetch()} />}
      {isCreation && <CreationSummary data={data} />}
      {data.execution_id ? <ExecutionStatus executionId={data.execution_id} proposalId={proposalId} /> : null}
      {!isCreation && (
        <div className={styles.row}>
          <div className={styles.block}>
            <span className={styles.blockLabel}>Datos ({data.data_window})</span>
            <span className={styles.dataAge}>{data.data_age_minutes === null ? "Antigüedad del dato no disponible" : `Dato de hace ${data.data_age_minutes} min`}</span>
          </div>

          <div className={styles.block}>
            <span className={styles.blockLabel}>Regla</span>
            <span className={styles.blockValue}>{data.rule_code ?? "—"}</span>
            <span className={styles.dataAge}>
              {data.rule_hit_rate_pct !== null
                ? `Acierto histórico ${data.rule_hit_rate_pct.toString().replace(".", ",")} % (${data.rule_hit_rate_sample} casos)`
                : "Sin histórico"}
            </span>
          </div>

          <div className={styles.block}>
            <span className={styles.blockLabel}>Impacto estimado</span>
            <span className={styles.blockValue}>
              {data.estimated_impact_range ? `${formatMoney(data.estimated_impact_range.low)} – ${formatMoney(data.estimated_impact_range.high)}` : "Sin intervalo estimado"}
            </span>
          </div>
        </div>
      )}

      {!isCreation && (
        <div className={styles.row}>
          {data.evidence.map((metric) => (
            <div className={styles.block} key={metric.metric}>
              <span className={styles.blockLabel}>
                {metric.metric} ({metric.window})
                {metric.target !== null ? ` · objetivo ${metric.target}` : ""}
              </span>
              {metric.actual !== undefined ? <span className={styles.blockValue}>{metric.actual}</span> : null}
              {metric.series.length ? <Sparkline values={metric.series.map((point) => point.value)} width={120} height={32} ariaLabel={`Evidencia de ${metric.metric}`} /> : null}
              <span className={styles.dataAge}>{metric.data_age_minutes === null ? "Antigüedad no disponible" : `Dato de hace ${metric.data_age_minutes} min`}</span>
            </div>
          ))}
        </div>
      )}

      {!isCreation && (
        <div className={styles.guardrails}>
          <span className={styles.blockLabel}>Límites de seguridad</span>
          {!data.guardrail_verdicts.length ? <span className={styles.dataAge}>Se validan en el servidor antes de ejecutar. No hay evaluación disponible en esta vista.</span> : null}
          {data.guardrail_verdicts.map((verdict) => (
            <span key={verdict.name} className={`${styles.guardrailRow} ${verdict.ok ? styles.guardrailOk : styles.guardrailFail}`}>
              {verdict.ok ? "✓" : "✕"} {verdict.name}: {formatVerdictValue(verdict)}
            </span>
          ))}
        </div>
      )}

      {data.entity_history.length > 0 ? (
        <div>
          <span className={styles.blockLabel}>Historial de la entidad</span>
          <ul className={styles.history}>
            {data.entity_history.map((entry, index) => (
              <li key={index} className={styles.historyItem} title={formatExactDate(entry.at)}>
                {formatRelativeTime(entry.at)} · {entry.actor_label} — {entry.summary}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {data.package_id ? <PackageLinkChip packageId={data.package_id} /> : null}

      {data.platform_url && isSafeUrl(data.platform_url) ? (
        <a className={styles.platformLink} href={data.platform_url} target="_blank" rel="noopener noreferrer">
          Abrir en la plataforma
        </a>
      ) : null}
    </div>
  );
}

function formatVerdictValue(verdict: { current: number; limit: number; unit: string }): string {
  if (verdict.unit === "money") return `${verdict.current} de ${verdict.limit} €`;
  if (verdict.unit === "percent") return `${verdict.current} % de ${verdict.limit} %`;
  return `${verdict.current} de ${verdict.limit}`;
}

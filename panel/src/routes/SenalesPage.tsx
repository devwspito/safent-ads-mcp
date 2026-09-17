import { useMe } from "@/api/queries/auth";
import { useSignals } from "@/api/queries/signals";
import type { SignalRow } from "@/api/schemas";
import { PageHeader } from "@/components/layout/PageHeader";
import { TableRegion } from "@/components/common/TableRegion";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { ReloadingIndicator } from "@/components/states/ReloadingIndicator";
import { SignalChip } from "@/components/signals/SignalChip";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { useSignalsFilters, type SinceOption } from "@/hooks/useSignalsFilters";
import { formatMoney } from "@/utils/money";
import { actionTakenLabel, outcomeLabel } from "@/utils/signals";
import { formatRelativeTime } from "@/utils/time";
import { platformLabel } from "@/utils/platform";
import styles from "./SenalesPage.module.css";

const KIND_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "BUY", label: "Subir" },
  { value: "HOLD", label: "Mantener" },
  { value: "SELL", label: "Bajar" },
  { value: "EXIT", label: "Salir" },
  { value: "FATIGUE", label: "Fatiga" },
  { value: "WINNER", label: "Ganadora" },
  { value: "LOSER", label: "Perdedora" },
  { value: "ANOMALY", label: "Anomalía" },
];

const STRENGTH_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "0", label: "Todas las fuerzas" },
  { value: "40", label: "Media o más (≥40)" },
  { value: "70", label: "Fuerte (≥70)" },
];

const SINCE_OPTIONS: Array<{ value: SinceOption; label: string }> = [
  { value: "hoy", label: "Hoy" },
  { value: "7d", label: "7 días" },
  { value: "14d", label: "14 días" },
  { value: "30d", label: "30 días" },
];

export function SenalesPage() {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  const { filters, sinceOption, setFilter } = useSignalsFilters();

  const signals = useSignals({
    business_id: businessId,
    platform: filters.platform,
    kind: filters.kind,
    min_strength: filters.minStrength,
    since: filters.since,
  });

  return (
    <div>
      <PageHeader
        title="Señales"
        actions={
          <div className={styles.toolbar}>
            <select
              className={styles.select}
              aria-label="Plataforma"
              value={filters.platform ?? ""}
              onChange={(e) => setFilter("platform", e.target.value || undefined)}
            >
              <option value="">Todas las plataformas</option>
              <option value="google">Google Ads</option>
              <option value="meta">Meta Ads</option>
            </select>

            <select
              className={styles.select}
              aria-label="Tipo de señal"
              value={filters.kind ?? ""}
              onChange={(e) => setFilter("kind", e.target.value || undefined)}
            >
              <option value="">Todos los tipos</option>
              {KIND_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>

            <select
              className={styles.select}
              aria-label="Fuerza mínima"
              value={String(filters.minStrength ?? 0)}
              onChange={(e) => setFilter("min_strength", e.target.value === "0" ? undefined : e.target.value)}
            >
              {STRENGTH_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>

            <select
              className={styles.select}
              aria-label="Desde cuándo"
              value={sinceOption ?? "30d"}
              onChange={(e) => setFilter("since", e.target.value)}
            >
              {SINCE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
        }
      />

      <QueryBoundary
        isLoading={signals.isLoading}
        isError={signals.isError}
        error={signals.error}
        onRetry={() => void signals.refetch()}
        data={signals.data}
        isEmpty={(data) => data.items.length === 0}
        emptyTitle="Sin señales en este ciclo"
        emptyBody="No hay señales accionables con los filtros actuales. El sistema sigue observando."
      >
        {(data) => (
          <>
            <div className={styles.headerBar}>
              {signals.isFetching && !signals.isLoading ? <ReloadingIndicator /> : <span />}
              {data.confirmed_rate_pct !== null ? (
                <span className={styles.confirmedRate}>
                  Tasa confirmada a 14 días: {data.confirmed_rate_pct.toString().replace(".", ",")} %
                </span>
              ) : null}
            </div>
            <SignalsTable signals={data.items} />
          </>
        )}
      </QueryBoundary>
    </div>
  );
}

function SignalsTable({ signals }: { signals: SignalRow[] }) {
  return (
    <TableRegion label="Tabla de señales">
    <table className={styles.table}>
      <caption className="visually-hidden">Señales del ciclo actual, ordenadas por emisión</caption>
      <thead>
        <tr>
          <th scope="col">Entidad</th>
          <th scope="col">Señal</th>
          <th scope="col">Causa</th>
          <th scope="col" className={styles.numeric}>
            Dinero en juego
          </th>
          <th scope="col">Qué se hizo</th>
          <th scope="col">Resultado a 14 d</th>
          <th scope="col">Emitida</th>
        </tr>
      </thead>
      <tbody>
        {signals.map((signal) => (
          <tr key={signal.signal_id}>
            <td>
              <div className={styles.entityCell}>
                <span className={styles.entityName}>{signal.entity_name}</span>
                <span className={styles.platformLabel}>{platformLabel(signal.platform)}</span>
              </div>
            </td>
            <td>
              <SignalChip kind={signal.kind} strength={signal.strength} />
            </td>
            <td className={styles.cause} title={signal.cause}>
              {signal.cause}
            </td>
            <td className={styles.numeric}>{formatMoney(signal.money_at_stake)}</td>
            <td>{actionTakenLabel(signal.action_taken)}</td>
            <td className={outcomeClassName(signal.outcome?.status)}>
              {signal.outcome ? outcomeLabel(signal.outcome.status, signal.outcome.days_remaining) : "—"}
            </td>
            <td>{formatRelativeTime(signal.emitted_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
    </TableRegion>
  );
}

function outcomeClassName(status: string | undefined): string {
  if (status === "confirmada") return styles.outcomeConfirmada ?? "";
  if (status === "no_confirmada") return styles.outcomeNoConfirmada ?? "";
  if (status === "en_curso") return styles.outcomeEnCurso ?? "";
  return "";
}

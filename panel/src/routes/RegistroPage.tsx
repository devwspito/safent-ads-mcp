import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMe } from "@/api/queries/auth";
import { useDecisionLog, useDecisionLogVerify } from "@/api/queries/decisionLog";
import type { DecisionLogEntry } from "@/api/schemas/decisionLog";
import { ChainBadge } from "@/components/registro/ChainBadge";
import { DecisionLogRow } from "@/components/registro/DecisionLogRow";
import { PageHeader } from "@/components/layout/PageHeader";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { ReloadingIndicator } from "@/components/states/ReloadingIndicator";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { downloadCsv, toCsv } from "@/utils/csv";
import styles from "./RegistroPage.module.css";

const EVENT_TYPE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "ProposalApproved", label: "Propuesta aprobada" },
  { value: "ExecutionSucceeded", label: "Cambio aplicado" },
  { value: "ExecutionUndone", label: "Cambio deshecho" },
  { value: "RuleFired", label: "Regla disparada" },
  { value: "EmergencyBrakeEngaged", label: "Freno activado" },
  { value: "EmergencyBrakeReleased", label: "Freno desactivado" },
];

export function RegistroPage() {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  const [searchParams, setSearchParams] = useSearchParams();
  const [expandedSeq, setExpandedSeq] = useState<number | null>(null);

  const eventType = searchParams.get("event_type") ?? undefined;
  const logQuery = useDecisionLog({ business_id: businessId, event_type: eventType });
  const verifyQuery = useDecisionLogVerify(businessId);

  function setEventTypeFilter(value: string) {
    setSearchParams((prev) => {
      const params = new URLSearchParams(prev);
      if (value) params.set("event_type", value);
      else params.delete("event_type");
      return params;
    });
  }

  function exportCsv(items: DecisionLogEntry[]) {
    const csv = toCsv(
      items.map((item) => ({
        seq: item.seq,
        tipo: item.event_type,
        entidad: item.entity_name ?? "",
        actor: item.actor_label,
        resumen: item.summary,
        fecha: item.occurred_at,
      })),
      ["seq", "tipo", "entidad", "actor", "resumen", "fecha"],
    );
    downloadCsv(`registro_${businessId}.csv`, csv);
  }

  return (
    <div>
      <PageHeader
        title="Historial"
        actions={
          <div className={styles.toolbar}>
            <label className="visually-hidden" htmlFor="registro-event-type-filter">
              Filtrar por tipo de evento
            </label>
            <select
              id="registro-event-type-filter"
              className={styles.select}
              value={eventType ?? ""}
              onChange={(e) => setEventTypeFilter(e.target.value)}
            >
              <option value="">Todos los tipos</option>
              {EVENT_TYPE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              className={styles.exportButton}
              disabled={!logQuery.data || logQuery.data.items.length === 0}
              onClick={() => logQuery.data && exportCsv(logQuery.data.items)}
            >
              Exportar CSV
            </button>
          </div>
        }
      />

      <div className={styles.chainRow}>
        <ChainBadge chainOk={verifyQuery.data?.chain_ok} checkedAt={verifyQuery.data?.checked_at} />
      </div>

      <QueryBoundary
        isLoading={logQuery.isLoading}
        isError={logQuery.isError}
        error={logQuery.error}
        onRetry={() => void logQuery.refetch()}
        data={logQuery.data}
        isEmpty={(data) => data.items.length === 0}
        emptyTitle="Sin entradas"
        emptyBody="No hay eventos en la bitácora con este filtro."
      >
        {(data) => (
          <>
            {logQuery.isFetching && !logQuery.isLoading ? <ReloadingIndicator /> : null}
            <div className={styles.log}>
              {data.items.map((entry) => (
                <DecisionLogRow
                  key={entry.seq}
                  entry={entry}
                  expanded={expandedSeq === entry.seq}
                  onToggle={() => setExpandedSeq((prev) => (prev === entry.seq ? null : entry.seq))}
                />
              ))}
            </div>
          </>
        )}
      </QueryBoundary>
    </div>
  );
}

import { useExecution } from "@/api/queries/executions";
import type { ExecutionOutcome } from "@/api/schemas/executions";
import styles from "./ExecutionStatus.module.css";

const labels: Record<ExecutionOutcome, string> = {
  CLAIMED: "Cambio en cola",
  RUNNING: "Aplicando cambio",
  UNKNOWN: "Resultado pendiente de confirmar",
  SUCCEEDED: "Cambio confirmado",
  FAILED: "Cambio fallido",
  SKIPPED_DRIFT: "No aplicado: la entidad ha cambiado",
  BLOCKED_GUARDRAIL: "Bloqueado por los límites de seguridad",
  BLOCKED_BRAKE: "Bloqueado por el freno de emergencia",
  UNDONE: "Cambio deshecho",
};

/** Persisted execution status, separate from the proposal's approval state. */
export function ExecutionStatus({ executionId, proposalId }: { executionId: string; proposalId: string }) {
  const { data, isError, isPending, isFetching, refetch } = useExecution(executionId, proposalId);
  const unknown = data?.outcome === "UNKNOWN";
  return (
    <section className={styles.status} aria-label="Estado de ejecución" data-uncertain={unknown || isError || undefined}>
      <div role="status" aria-live="polite">
        <p className={styles.title}>{data ? labels[data.outcome] : isError ? "Estado de ejecución no disponible" : "Consultando ejecución…"}</p>
        {unknown ? (
          <p className={styles.description}>
            La plataforma puede haber aplicado el cambio, pero aún no tenemos confirmación.
            Safent mantiene la reserva de seguridad y consulta el recibo sin repetir la operación.
            No se puede reintentar ni deshacer hasta resolver el resultado.
          </p>
        ) : null}
        {isError ? <p className={styles.description}>No se pudo actualizar el estado. {data ? "El resultado mostrado es el último conocido. " : ""}Esto no confirma un fallo del cambio.</p> : null}
      </div>
      {!isPending || isError ? (
        <button type="button" className={styles.refresh} disabled={isFetching} onClick={() => void refetch()}>
          {isFetching ? "Consultando…" : "Actualizar estado"}
        </button>
      ) : null}
    </section>
  );
}

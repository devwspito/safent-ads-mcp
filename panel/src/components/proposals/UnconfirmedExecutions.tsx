import { useUnconfirmedExecutions } from "@/api/queries/executions";
import styles from "./ExecutionStatus.module.css";

/** Persist across reloads: executing proposals no longer appear in the pending inbox. */
export function UnconfirmedExecutions({ businessId }: { businessId: string }) {
  const { data, isError, isFetching, refetch } = useUnconfirmedExecutions(businessId);
  if (!isError && !data?.items.length) return null;
  return <section className={`${styles.status} ${styles.inboxNotice}`} aria-label="Cambios sin confirmar" data-uncertain>
    <div>
      <p className={styles.title}>Cambios pendientes de confirmar</p>
      {isError ? <p role="status" className={styles.description}>No se pudo consultar el estado de ejecución. {data ? "Se muestra la última lista conocida, sin verificar." : "No podemos confirmar si hay cambios pendientes de resolver."}</p> : null}
      {data?.items.length ? <>
        <p className={styles.description}>La plataforma puede haber aplicado estos cambios. Safent mantiene las reservas de seguridad mientras consulta sus recibos; no repitas la operación.</p>
        <ul className={styles.description}>{data.items.map((item) => <li key={item.execution_id}>{item.entity_name} — resultado pendiente de confirmar</li>)}</ul>
        {data.items.length === 200 ? <p className={styles.description}>Se muestran hasta 200 ejecuciones. Puede haber más pendientes.</p> : null}
      </> : null}
    </div>
    <button type="button" className={styles.refresh} disabled={isFetching} onClick={() => void refetch()}>{isFetching ? "Consultando…" : "Actualizar estado"}</button>
  </section>;
}

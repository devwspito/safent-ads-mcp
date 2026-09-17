import styles from "./FreshnessBadge.module.css";

interface FreshnessBadgeProps {
  lagMinutes: number | undefined;
  // Additive (hotfix 0.2.20, Bug B): no metrics ingested yet, not stale data.
  noData?: boolean;
}

/**
 * Sello de frescura — panel-visual-spec.md §4:
 * <15 min sin icono · 15–60 min con reloj · >60 min ficha ámbar · desconocido borde discontinuo.
 */
export function FreshnessBadge({ lagMinutes, noData }: FreshnessBadgeProps) {
  if (noData) {
    return <span className={`${styles.badge} ${styles.unknown}`} title="Todavía no se han recibido estadísticas de todas las cuentas">Sin estadísticas todavía</span>;
  }
  // Defensive: an old backend response may still carry the legacy sentinel
  // for "never ingested" instead of `no_data`. Never render it as a
  // fictitious age of tens of thousands of hours.
  if (lagMinutes !== undefined && lagMinutes >= 1_000_000) {
    return <span className={`${styles.badge} ${styles.unknown}`} title="Todavía no se han recibido estadísticas de todas las cuentas">Estadísticas pendientes</span>;
  }
  if (lagMinutes === undefined) {
    return (
      <span className={`${styles.badge} ${styles.unknown}`} title="Frescura desconocida">
        Frescura desconocida
      </span>
    );
  }
  if (lagMinutes > 60) {
    const hours = Math.floor(lagMinutes / 60);
    return <span className={`${styles.badge} ${styles.stale}`}>Datos de hace {hours} h</span>;
  }
  if (lagMinutes >= 15) {
    return (
      <span className={`${styles.badge} ${styles.aging}`}>
        <ClockIcon /> Datos de hace {lagMinutes} min
      </span>
    );
  }
  return <span className={`${styles.badge} ${styles.fresh}`}>Datos de hace {lagMinutes} min</span>;
}

function ClockIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
      <circle cx="6" cy="6" r="5" stroke="currentColor" strokeWidth="1" />
      <path d="M6 3.5V6l2 1" stroke="currentColor" strokeWidth="1" strokeLinecap="round" />
    </svg>
  );
}

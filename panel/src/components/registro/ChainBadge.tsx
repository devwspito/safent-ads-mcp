import { formatRelativeTime } from "@/utils/time";
import styles from "./ChainBadge.module.css";

interface ChainBadgeProps {
  chainOk: boolean | undefined;
  checkedAt: string | undefined;
}

/**
 * Insignia de la cadena de hash — `rota` interrumpe (incidente de integridad), las demás
 * son informativas — panel-interaction-spec.md §3.7.
 */
export function ChainBadge({ chainOk, checkedAt }: ChainBadgeProps) {
  if (chainOk === undefined) {
    return (
      <span className={`${styles.badge} ${styles.pending}`}>
        <span className={styles.dot} aria-hidden="true" />
        Verificación pendiente
      </span>
    );
  }
  if (!chainOk) {
    return (
      <div className={`${styles.badge} ${styles.broken}`} role="alert">
        <span className={styles.dot} aria-hidden="true" />
        Cadena rota: incidente de integridad. Contacta con soporte antes de confiar en este registro.
      </div>
    );
  }
  return (
    <span className={`${styles.badge} ${styles.verified}`}>
      <span className={styles.dot} aria-hidden="true" />
      Cadena verificada {checkedAt ? formatRelativeTime(checkedAt) : ""}
    </span>
  );
}

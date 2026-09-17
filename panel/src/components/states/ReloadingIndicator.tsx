import styles from "./ReloadingIndicator.module.css";

/**
 * Indicador sutil de refresco de fondo — el dato anterior permanece visible.
 * Nunca vacía el contenido leído (panel-interaction-spec.md §2).
 */
export function ReloadingIndicator() {
  return (
    <span className={styles.indicator} role="status" aria-live="polite">
      <span className={styles.spinner} aria-hidden="true" />
      Actualizando…
    </span>
  );
}

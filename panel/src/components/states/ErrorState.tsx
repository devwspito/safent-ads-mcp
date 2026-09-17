import styles from "./ErrorState.module.css";

interface ErrorStateProps {
  message: string;
  onRetry: () => void;
}

/** Error de nuestra API: lenguaje llano y `Reintentar` que repite esa petición — panel-interaction-spec.md §2. */
export function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <div className={styles.wrap} role="alert">
      <svg className={styles.icon} viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.5" />
        <path d="M12 8v5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        <circle cx="12" cy="16" r="0.75" fill="currentColor" />
      </svg>
      <p className={styles.title}>No se ha podido cargar</p>
      <p className={styles.body}>{message}</p>
      <button type="button" className={styles.retry} onClick={onRetry}>
        Reintentar
      </button>
    </div>
  );
}

import { Modal } from "./Modal";
import styles from "./TypedConfirmDialog.module.css";

interface Props {
  title: string;
  description: string;
  confirmLabel: string;
  summary: string[];
  canConfirm: boolean;
  errorMessage?: string | null;
  isSubmitting?: boolean;
  onConfirm: () => void;
  onClose: () => void;
  /** Ver `Modal.returnFocusTo` — imprescindible cuando este diálogo es el segundo de una cadena. */
  returnFocusTo?: { current: HTMLElement | null };
}

/** A human decision, not a second login. Summary is the prepared snapshot. */
export function ActionConfirmationDialog({ title, description, confirmLabel, summary,
  canConfirm, errorMessage, isSubmitting = false, onConfirm, onClose, returnFocusTo }: Props) {
  return <Modal label={title} onClose={onClose} busy={isSubmitting} returnFocusTo={returnFocusTo}>
    <div className={styles.body}>
      <p className={styles.title}>{title}</p>
      <p className={styles.copy}>{description}</p>
      <div className={styles.field} aria-label="Acción que confirmas">
        {summary.map((line, index) => <p key={index} className={styles.copy}>{line}</p>)}
      </div>
      {errorMessage ? <p className={styles.copy} role="alert">{errorMessage}</p> : null}
      <div className={styles.actions}>
        <button type="button" className={styles.cancel} onClick={onClose} disabled={isSubmitting}>Cancelar</button>
        <button type="button" className={styles.confirm} onClick={onConfirm} disabled={!canConfirm || isSubmitting}>
          {isSubmitting ? "Verificando…" : confirmLabel}
        </button>
      </div>
    </div>
  </Modal>;
}

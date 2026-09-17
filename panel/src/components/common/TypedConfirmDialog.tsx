import { useId, useRef, useState } from "react";
import { describeApiError } from "@/utils/apiError";
import { Modal } from "./Modal";
import styles from "./TypedConfirmDialog.module.css";

interface TypedConfirmDialogProps {
  title: string;
  description: string;
  confirmLabel: string;
  cancelLabel?: string;
  /** Si se indica, el botón de confirmar permanece deshabilitado hasta que se teclee exactamente esta palabra. */
  confirmWord?: string;
  reasonRequired?: boolean;
  reasonLabel?: string;
  reasonPlaceholder?: string;
  danger?: boolean;
  onConfirm: (reason: string) => void | Promise<unknown>;
  onClose: () => void;
}

/**
 * Confirmación tecleada reutilizable — irreversibles piden re-tecleo, no solo un clic
 * (panel-interaction-spec.md §0.3, §3.4, §3.6). Usado por freno, tope mensual y
 * publicar creatividad.
 */
export function TypedConfirmDialog({
  title,
  description,
  confirmLabel,
  cancelLabel = "Cancelar",
  confirmWord,
  reasonRequired = false,
  reasonLabel = "Motivo",
  reasonPlaceholder,
  danger = false,
  onConfirm,
  onClose,
}: TypedConfirmDialogProps) {
  const [reason, setReason] = useState("");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitted = useRef(false);
  const reasonId = useId();
  const typedId = useId();

  const wordOk = !confirmWord || typed.trim().toUpperCase() === confirmWord.toUpperCase();
  const reasonOk = !reasonRequired || reason.trim().length > 0;
  const canConfirm = wordOk && reasonOk && !busy;

  return (
    <Modal label={title} onClose={onClose} busy={busy}>
      <div className={styles.body}>
        <p className={styles.title}>{title}</p>
        <p className={styles.copy}>{description}</p>

        {reasonRequired || reasonPlaceholder ? (
          <div className={styles.field}>
            <label className={styles.label} htmlFor={reasonId}>
              {reasonLabel}
            </label>
            <input
              id={reasonId}
              className={styles.input}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder={reasonPlaceholder}
              autoFocus={!confirmWord}
            />
          </div>
        ) : null}

        {confirmWord ? (
          <div className={styles.field}>
            <label className={styles.label} htmlFor={typedId}>
              Escribe {confirmWord} para confirmar
            </label>
            <input
              id={typedId}
              className={styles.input}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              autoComplete="off"
              autoFocus
            />
          </div>
        ) : null}

        {error ? <p role="alert" className={styles.error}>{error}</p> : null}
        <div className={styles.actions}>
          <button type="button" className={styles.cancel} onClick={onClose} disabled={busy}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`${styles.confirm} ${danger ? styles.confirmDanger : ""}`}
            onClick={async () => {
              if (submitted.current || !canConfirm) return;
              submitted.current = true;
              setBusy(true);
              setError(null);
              try { await onConfirm(reason); }
              catch (failure) { setError(describeApiError(failure)); }
              finally { submitted.current = false; setBusy(false); }
            }}
            disabled={!canConfirm}
          >
            {busy ? "Guardando…" : confirmLabel}
          </button>
        </div>
      </div>
    </Modal>
  );
}

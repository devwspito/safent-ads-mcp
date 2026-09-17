import { useRef, useState } from "react";
import { Modal } from "@/components/common/Modal";
import { describeApiError } from "@/utils/apiError";
import styles from "./DeleteCampaignSheet.module.css";

interface DeleteCampaignSheetProps {
  campaignName: string;
  platformLabel: string;
  onDelete: () => Promise<unknown>;
  onPauseInstead: () => void;
  onClose: () => void;
}

/**
 * Hoja de eliminar campaña — design.md §8.4: repite el nombre real, dice qué se pierde y ofrece
 * "Pausar en su lugar" como salida segura y reversible al lado del botón peligroso, en vez de
 * pedir teclear una palabra (aquí sí hay una alternativa más segura que ofrecer en el mismo paso).
 */
export function DeleteCampaignSheet({ campaignName, platformLabel, onDelete, onPauseInstead, onClose }: DeleteCampaignSheetProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitted = useRef(false);

  async function handleDelete() {
    if (submitted.current) return;
    submitted.current = true;
    setBusy(true);
    setError(null);
    try {
      await onDelete();
    } catch (failure) {
      setError(describeApiError(failure));
      submitted.current = false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal label={`Eliminar «${campaignName}»`} onClose={onClose} busy={busy}>
      <div className={styles.body}>
        <p className={styles.title}>Eliminar «{campaignName}»</p>
        <p className={styles.copy}>
          Se eliminará de {platformLabel} y dejará de gastar. Se pierden su historial de resultados y cualquier propuesta pendiente sobre ella. No se puede deshacer.
        </p>
        {error ? <p className={styles.error} role="alert">No se pudo eliminar: {error}</p> : null}
        <div className={styles.actions}>
          <button type="button" className={styles.delete} disabled={busy} onClick={() => void handleDelete()}>
            {busy ? "Eliminando…" : "Eliminar"}
          </button>
          <button type="button" className={styles.pauseInstead} disabled={busy} onClick={onPauseInstead}>
            Pausar en su lugar
          </button>
          <button type="button" className={styles.cancel} disabled={busy} onClick={onClose}>
            Cancelar
          </button>
        </div>
      </div>
    </Modal>
  );
}

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { usePackageDetail, useResumePackage, useUndoPackage } from "@/api/queries/packages";
import styles from "./PublicationProgress.module.css";

interface PublicationProgressProps {
  businessId: string;
  packageId: string;
}

/**
 * Tira de progreso que sustituye al dinero y a los botones de la fila una vez aprobado el
 * paquete — `contracts/api.md` §7 "Después de aprobar". Región `aria-live="polite"` única:
 * design.md §12.3 pide anunciar el cambio de estado, no repetir la cuenta atrás cada segundo,
 * así que el número de segundos vive en un `<span>` aparte del texto que cambia por estado.
 */
export function PublicationProgress({ businessId, packageId }: PublicationProgressProps) {
  const detail = usePackageDetail(businessId, packageId);
  const resumeMutation = useResumePackage(businessId);
  const undoMutation = useUndoPackage(businessId);
  const [tick, setTick] = useState(() => Date.now());
  const [actionError, setActionError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const publication = detail.data?.publication ?? null;
  const isCountingDown = publication?.state === "pending" || (publication?.state === "completed" && publication.undo_deadline !== null);

  useEffect(() => {
    if (!isCountingDown) return;
    const timer = setInterval(() => setTick(Date.now()), 250);
    return () => clearInterval(timer);
  }, [isCountingDown]);

  if (detail.isLoading || !detail.data) {
    return (
      <div className={styles.wrap} role="status" aria-live="polite">
        Consultando la publicación…
      </div>
    );
  }

  if (detail.isError || !publication) {
    return (
      <div className={styles.wrap} role="alert">
        <span>No se pudo consultar el estado.</span>
        <button type="button" className={styles.linkButton} onClick={() => void detail.refetch()}>
          Reintentar
        </button>
      </div>
    );
  }

  const secondsLeft = publication.undo_deadline ? Math.max(0, Math.ceil((new Date(publication.undo_deadline).getTime() - tick) / 1000)) : 0;

  async function handleUndo() {
    setActionError(null);
    setPending(true);
    try {
      await undoMutation.mutateAsync({ packageId, packageHash: detail.data!.package_hash, reason: "Deshecho desde Propuestas" });
    } catch {
      setActionError("No se pudo deshacer.");
    } finally {
      setPending(false);
    }
  }

  async function handleResume() {
    setActionError(null);
    setPending(true);
    try {
      await resumeMutation.mutateAsync({ packageId, packageHash: detail.data!.package_hash });
    } catch {
      setActionError("No se pudo continuar.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className={styles.wrap} role="status" aria-live="polite" aria-label="Progreso de la publicación">
      {publication.state === "pending" ? (
        <>
          <p className={styles.sentence}>Aprobado. Puedes cancelarlo entero durante unos segundos.</p>
          <button type="button" className={styles.undoButton} disabled={pending} onClick={() => void handleUndo()}>
            {pending ? "Cancelando…" : "Deshacer"} <span aria-hidden="true" className={styles.seconds}>({secondsLeft} s)</span>
          </button>
        </>
      ) : null}

      {publication.state === "running" ? (
        <p className={styles.sentence} aria-busy="true">
          {publication.progress_sentence}
        </p>
      ) : null}

      {publication.state === "completed" ? (
        <div className={styles.outcome} data-tone="good">
          <p className={styles.sentence}>Campaña publicada y activa.</p>
          <div className={styles.outcomeActions}>
            {publication.undo_deadline ? (
              <button type="button" className={styles.undoButton} disabled={pending} onClick={() => void handleUndo()}>
                {pending ? "Pausando…" : "Deshacer"} <span aria-hidden="true" className={styles.seconds}>({secondsLeft} s)</span>
              </button>
            ) : null}
            <Link to="/campanas" className={styles.link}>
              Ver en Campañas
            </Link>
          </div>
        </div>
      ) : null}

      {publication.state === "halted" && detail.data.state === "partially_published" ? (
        <div className={styles.outcome} data-tone="warning">
          <p className={styles.sentence}>{publication.halted!.reason_plain}</p>
          <div className={styles.outcomeActions}>
            {publication.halted!.can_resume ? (
              <button type="button" className={styles.continueButton} disabled={pending} onClick={() => void handleResume()}>
                {pending ? "Continuando…" : "Continuar"}
              </button>
            ) : null}
            <Link to="/campanas" className={styles.link}>
              Ver en Campañas
            </Link>
          </div>
        </div>
      ) : null}

      {publication.state === "halted" && detail.data.state === "failed" ? (
        <div className={styles.outcome} data-tone="bad">
          <p className={styles.sentence}>No se ha podido crear la campaña. No se ha creado nada ni se ha gastado nada.</p>
          <details className={styles.whyDetails}>
            <summary>Ver por qué</summary>
            <p>{publication.halted!.reason_plain}</p>
          </details>
        </div>
      ) : null}

      {actionError ? (
        <p role="alert" className={styles.actionError}>
          {actionError}
        </p>
      ) : null}
    </div>
  );
}

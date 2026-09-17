import { useEffect, useId, useState } from "react";
import type { ReauthMethod } from "@/utils/apiError";
import { formatTimeHHMM } from "@/utils/time";
import { Modal } from "./Modal";
import styles from "./TypedConfirmDialog.module.css";

interface FreshIdentificationPromptProps {
  title: string;
  description: string;
  confirmLabel: string;
  /** Vías que este dueño puede usar de verdad (contracts/federated-login.md §4). */
  methods: ReauthMethod[];
  /** `useMe().session.fresh_identification_until` — solo se pinta si sigue en el futuro. */
  freshUntil?: string | null;
  errorMessage?: string | null;
  isSubmitting?: boolean;
  isStartingGoogle?: boolean;
  onConfirmTotp: (code: string) => void;
  onConfirmWithGoogle: () => void;
  onClose: () => void;
  /** Ver `Modal.returnFocusTo` — imprescindible cuando este prompt es el primero de una cadena. */
  returnFocusTo?: { current: HTMLElement | null };
}

const TOTP_CODE_LENGTH = 6;
const TOTP_CODE_PATTERN = /^\d{6}$/;

/** Un instante ya pasado no es información útil aquí — evita anunciar una frescura que ya caducó. */
function freshUntilLabel(freshUntil: string | null | undefined): string | null {
  if (!freshUntil) return null;
  const until = Date.parse(freshUntil);
  if (Number.isNaN(until) || until <= Date.now()) return null;
  return `Identificado hasta las ${formatTimeHHMM(freshUntil)}`;
}

/**
 * Prueba de presencia reutilizable (`X-Reauth-Token` o identificación federada fresca,
 * contracts/federated-login.md §4): un solo componente con hasta dos vías según
 * `details.methods` del 401 — el campo de 6 dígitos de siempre, un botón «Confirmar con
 * Google», o ambos. Vive en `components/common` porque `useFreshIdentification` (`hooks/`) lo
 * monta desde varias mutaciones sensibles del panel. Sustituye a `TotpReauthPrompt`.
 */
export function FreshIdentificationPrompt({
  title,
  description,
  confirmLabel,
  methods,
  freshUntil,
  errorMessage,
  isSubmitting = false,
  isStartingGoogle = false,
  onConfirmTotp,
  onConfirmWithGoogle,
  onClose,
  returnFocusTo,
}: FreshIdentificationPromptProps) {
  const [code, setCode] = useState("");
  const [, forceTickAtExpiry] = useState(0);
  const fieldId = useId();
  const showTotp = methods.includes("totp");
  const showGoogle = methods.includes("federated");
  const canConfirmTotp = TOTP_CODE_PATTERN.test(code) && !isSubmitting;
  const freshLabel = freshUntilLabel(freshUntil);

  // El diálogo puede quedarse abierto (p.ej. escribiendo el TOTP) hasta después de que
  // `freshUntil` pase: sin este tic, «Identificado hasta las HH:MM» seguiría a la vista con la
  // hora ya caducada, en vez de desaparecer sola en cuanto deja de ser cierto.
  useEffect(() => {
    if (!freshUntil) return;
    const until = Date.parse(freshUntil);
    const delay = until - Date.now();
    if (Number.isNaN(until) || delay <= 0) return;
    const timer = setTimeout(() => forceTickAtExpiry((tick) => tick + 1), delay);
    return () => clearTimeout(timer);
  }, [freshUntil]);

  return (
    <Modal label={title} onClose={onClose} returnFocusTo={returnFocusTo}>
      <div className={styles.body}>
        <p className={styles.title}>{title}</p>
        <p className={styles.copy}>{description}</p>
        {freshLabel ? <p className={styles.copy}>{freshLabel}</p> : null}

        {showGoogle ? (
          <button
            type="button"
            className={styles.confirm}
            onClick={onConfirmWithGoogle}
            disabled={isStartingGoogle}
          >
            {isStartingGoogle ? "Abriendo Google…" : "Confirmar con Google"}
          </button>
        ) : null}

        {showGoogle && showTotp ? (
          <p className={styles.copy}>o escribe el código de tu aplicación de autenticación</p>
        ) : null}

        {showTotp ? (
          <div className={styles.field}>
            <label className={styles.label} htmlFor={fieldId}>
              Código de verificación (6 dígitos)
            </label>
            <input
              id={fieldId}
              className={styles.input}
              value={code}
              onChange={(event) => setCode(event.target.value.replace(/\D/g, "").slice(0, TOTP_CODE_LENGTH))}
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={TOTP_CODE_LENGTH}
              autoFocus={!showGoogle}
            />
          </div>
        ) : null}

        {errorMessage ? (
          <span className={styles.copy} role="alert">
            {errorMessage}
          </span>
        ) : null}

        <div className={styles.actions}>
          <button type="button" className={styles.cancel} onClick={onClose}>
            Cancelar
          </button>
          {showTotp ? (
            <button
              type="button"
              className={styles.confirm}
              onClick={() => onConfirmTotp(code)}
              disabled={!canConfirmTotp}
            >
              {isSubmitting ? "Confirmando…" : confirmLabel}
            </button>
          ) : null}
        </div>
      </div>
    </Modal>
  );
}

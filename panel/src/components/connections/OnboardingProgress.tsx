import type { OnboardingStatus, OnboardingStepId, OnboardingStepStatus } from "@/api/schemas/onboarding";
import styles from "./OnboardingProgress.module.css";

interface OnboardingProgressProps {
  status: OnboardingStatus | undefined;
  isLoading: boolean;
}

const STEP_LABEL: Record<OnboardingStepId, string> = {
  google_app: "Cliente OAuth de Google Ads",
  google_account: "Cuenta de Google Ads conectada",
  meta_app: "App de Meta Ads",
  meta_account: "Cuenta de Meta Ads conectada",
};

const STATUS_LABEL: Record<OnboardingStepStatus, string> = {
  done: "Listo",
  pending: "Pendiente",
  blocked: "Bloqueado",
};

const BLOCKING_REASON_LABEL: Record<string, string> = {
  google_app_not_configured: "Configura antes el cliente OAuth de Google Ads.",
  meta_app_not_configured: "Configura antes la app de Meta Ads.",
};

/**
 * Resumen del onboarding (029 US2): lee `GET /onboarding` y muestra el
 * estado real de cada paso -- ningún estado propio del componente, así que
 * recargar la página deja el progreso exactamente donde estaba (resumible).
 * No sustituye ni duplica las tarjetas de abajo: solo señala cuál falta.
 */
export function OnboardingProgress({ status, isLoading }: OnboardingProgressProps) {
  if (isLoading || !status) return null;

  return (
    <section className={styles.section} aria-label="Progreso de la configuración">
      <h2 className={styles.title}>
        {status.complete ? "Anuncios está listo" : "Progreso de la configuración"}
      </h2>
      <ol className={styles.steps}>
        {status.steps.map((step) => (
          <li key={step.id} className={styles.step}>
            <span className={`${styles.badge} ${styles[step.status]}`}>{STATUS_LABEL[step.status]}</span>
            <span className={styles.label}>{STEP_LABEL[step.id]}</span>
            {step.blocking_reason ? (
              <span className={styles.reason}>{BLOCKING_REASON_LABEL[step.blocking_reason] ?? step.blocking_reason}</span>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
  );
}

import { formatExactDate } from "@/utils/time";
import styles from "./Banner.module.css";

interface KillSwitchBannerProps {
  engagedAt: string;
  reason: string | null;
  onRequestRelease: () => void;
}

/**
 * Cambios parados: franja persistente no descartable — panel-interaction-spec.md §0.7, §3.3.
 * Reanudar exige confirmación tecleada (la resuelve quien invoque onRequestRelease).
 */
export function KillSwitchBanner({ engagedAt, reason, onRequestRelease }: KillSwitchBannerProps) {
  return (
    <div className={`${styles.banner} ${styles.brake}`} role="alert">
      <span className={styles.dot} aria-hidden="true" />
      <span>
        Los cambios están parados desde {formatExactDate(engagedAt)}. Nada se aplicará hasta que los reanudes.
        {reason ? <span className={styles.reason}> {reason}</span> : null}
      </span>
      <button type="button" className={styles.action} onClick={onRequestRelease}>
        Reanudar los cambios
      </button>
    </div>
  );
}

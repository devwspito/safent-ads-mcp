import { staleDataLabel } from "@/utils/time";
import styles from "./Banner.module.css";

interface StaleBannerProps {
  lagMinutes: number;
}

/** Frescura > 60 min (NFR-1): franja ámbar, escritura deshabilitada — panel-interaction-spec.md §2. */
export function StaleBanner({ lagMinutes }: StaleBannerProps) {
  return (
    <div className={`${styles.banner} ${styles.stale}`} role="status">
      <span className={styles.dot} aria-hidden="true" />
      <span>{staleDataLabel(lagMinutes)}. Escritura deshabilitada mientras dure.</span>
    </div>
  );
}

import styles from "./Banner.module.css";

interface DegradedBannerProps {
  accountLabel: string;
  reason: string;
}

/** Una cuenta cae: pasa a solo lectura, las demás siguen operativas (NFR-10) — panel-interaction-spec.md §2. */
export function DegradedBanner({ accountLabel, reason }: DegradedBannerProps) {
  return (
    <div className={`${styles.banner} ${styles.degraded}`} role="status">
      <span className={styles.dot} aria-hidden="true" />
      <span>
        {accountLabel}: {reason} <span className={styles.reason}>Las demás cuentas siguen operativas.</span>
      </span>
    </div>
  );
}

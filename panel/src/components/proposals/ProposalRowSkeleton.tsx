import { Skeleton } from "@/components/states/Skeleton";
import styles from "./ProposalRowSkeleton.module.css";

/**
 * Esqueleto de tarjeta con la línea de acciones dibujada — panel-interaction-spec.md §3.3:
 * "tres esqueletos de tarjeta de 96 px con la línea de acciones dibujada".
 */
export function ProposalRowSkeleton() {
  return (
    <div className={styles.card} aria-hidden="true">
      <div className={styles.content}>
        <Skeleton height="16px" width="70%" />
        <Skeleton height="12px" width="45%" />
        <Skeleton height="12px" width="85%" />
      </div>
      <div className={styles.actions}>
        <Skeleton height="36px" width="84px" />
        <Skeleton height="20px" width="64px" />
      </div>
    </div>
  );
}

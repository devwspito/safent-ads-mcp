import type { ReactNode } from "react";
import styles from "./EmptyState.module.css";

interface EmptyStateProps {
  title: string;
  body?: string;
  action?: ReactNode;
}

/** Vacío: qué iría aquí y una acción para poblarlo — panel-interaction-spec.md §2. */
export function EmptyState({ title, body, action }: EmptyStateProps) {
  return (
    <div className={styles.wrap} role="status">
      <span className={styles.iconWrap} aria-hidden="true">
        <svg className={styles.icon} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" strokeWidth="1.5" />
          <path d="M8 12h8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      </span>
      <p className={styles.title}>{title}</p>
      {body ? <p className={styles.body}>{body}</p> : null}
      {action ? <div className={styles.action}>{action}</div> : null}
    </div>
  );
}

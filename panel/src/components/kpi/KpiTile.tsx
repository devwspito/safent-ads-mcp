import type { ReactNode } from "react";
import { Sparkline } from "./Sparkline";
import styles from "./KpiTile.module.css";

interface KpiTileProps {
  label: string;
  value: string;
  delta?: { text: string; valence: "good" | "bad" | "neutral" | "spend-up" | "spend-down" };
  context?: string;
  sparkline?: number[];
  highlight?: boolean;
  action?: ReactNode;
}

/**
 * Ficha KPI: tres pesos visuales, cifra > etiqueta > contexto — panel-visual-spec.md §4.
 * Máximo 6 por fila (panel-interaction-spec.md §3.1).
 */
export function KpiTile({ label, value, delta, context, sparkline, highlight, action }: KpiTileProps) {
  const deltaClass = delta
    ? {
        good: styles.deltaGood,
        bad: styles.deltaBad,
        neutral: styles.deltaNeutral,
        "spend-up": styles.deltaSpendUp,
        "spend-down": styles.deltaSpendDown,
      }[delta.valence]
    : "";

  return (
    <div className={`${styles.tile} ${highlight ? styles.highlight : ""}`}>
      <span className={styles.label}>{label}</span>
      <div className={styles.row}>
        <span className={styles.value}>{value}</span>
        {sparkline ? <Sparkline values={sparkline} ariaLabel={`Tendencia de ${label} en 14 días`} /> : null}
      </div>
      {delta ? <span className={`${styles.delta} ${deltaClass}`}>{delta.text}</span> : null}
      {context ? <span className={styles.context}>{context}</span> : null}
      {action}
    </div>
  );
}

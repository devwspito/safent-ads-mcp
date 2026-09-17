import { useState } from "react";
import type { GraceEntry } from "@/hooks/useServerGraceUndo";
import styles from "./UndoBar.module.css";

interface UndoBarProps {
  entries: GraceEntry[];
  secondsLeft: (entry: GraceEntry) => number;
  onUndo: (id: string) => void;
  onUndoAll?: () => void;
}

/**
 * Un único aviso «Hecho» que acumula — panel-interaction-spec.md §2.4: "3 cambios hechos ·
 * Deshacer todos (18 s)", con la lista desplegable de los tres. Ventana de gracia servidora,
 * no un temporizador de cliente. Salir de la vista confirma, no pierde.
 */
export function UndoBar({ entries, secondsLeft, onUndo, onUndoAll }: UndoBarProps) {
  const [expanded, setExpanded] = useState(false);
  if (entries.length === 0) return null;

  const soonest = entries.reduce((min, entry) => (secondsLeft(entry) < secondsLeft(min) ? entry : min), entries[0]!);
  const isSingle = entries.length === 1;

  return (
    <div className={styles.wrap} role="region" aria-label="Cambios recientes con deshacer disponible">
      <div className={styles.row} role="status">
        <span className={styles.label}>{isSingle ? entries[0]!.label : `${entries.length} cambios hechos`}</span>
        <span className={styles.time}>{formatSeconds(secondsLeft(soonest))}</span>
        <button
          type="button"
          className={styles.button}
          onClick={() => (isSingle ? onUndo(entries[0]!.id) : onUndoAll ? onUndoAll() : entries.forEach((entry) => onUndo(entry.id)))}
        >
          {isSingle ? "Deshacer" : `Deshacer todos (${entries.length})`}
        </button>
        {!isSingle ? (
          <button type="button" className={styles.expandToggle} aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
            {expanded ? "Ocultar" : "Ver"}
          </button>
        ) : null}
      </div>

      {!isSingle && expanded ? (
        <ul className={styles.detailList}>
          {entries.map((entry) => (
            <li key={entry.id} className={styles.detailItem}>
              <span className={styles.label}>{entry.label}</span>
              <span className={styles.time}>{formatSeconds(secondsLeft(entry))}</span>
              <button type="button" className={styles.button} onClick={() => onUndo(entry.id)}>
                Deshacer
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function formatSeconds(totalSeconds: number): string {
  return `${totalSeconds} s`;
}

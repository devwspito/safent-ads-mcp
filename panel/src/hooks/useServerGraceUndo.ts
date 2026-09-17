/**
 * Cola de deshacer con ventana de gracia SERVIDORA — sustituye a `useUndoQueue` de
 * oposads-agent (que corría un `setTimeout` local). Aquí el reloj lo manda el backend:
 * cada entrada trae su propio `deadline` (`execution_scheduled_at`) y el hook sólo cuenta
 * hacia atrás y expone `undo()`, que llama a `POST /executions/{id}/undo`.
 * panel-interaction-spec.md §3.4, §8.
 */
import { useCallback, useEffect, useRef, useState } from "react";

export interface GraceEntry {
  id: string;
  executionId: string;
  label: string;
  deadline: number;
}

export interface UseServerGraceUndoOptions {
  scopeKey?: string;
  onUndo: (executionId: string) => Promise<void>;
  /** `POST /executions/undo` — un solo asidero para deshacer todo un lote (rest-api.md §Ejecución). */
  onUndoBatch?: (executionIds: string[]) => Promise<void>;
}

export function useServerGraceUndo({ onUndo, onUndoBatch, scopeKey = "" }: UseServerGraceUndoOptions) {
  const [entries, setEntries] = useState<GraceEntry[]>([]);
  const [now, setNow] = useState(() => Date.now());
  const scopeRef = useRef(scopeKey);
  scopeRef.current = scopeKey;
  useEffect(() => { setEntries([]); }, [scopeKey]);

  useEffect(() => {
    if (entries.length === 0) return;
    const timer = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(timer);
  }, [entries.length]);

  useEffect(() => {
    setEntries((prev) => prev.filter((entry) => entry.deadline > now));
  }, [now]);

  const enqueue = useCallback((entry: Omit<GraceEntry, "id"> & { id?: string }) => {
    if (scopeRef.current !== scopeKey) return;
    const id = entry.id ?? entry.executionId;
    setEntries((prev) => [...prev.filter((e) => e.id !== id), { ...entry, id }]);
  }, [scopeKey]);

  const dismiss = useCallback((id: string) => {
    setEntries((prev) => prev.filter((e) => e.id !== id));
  }, []);

  const undo = useCallback(
    async (id: string) => {
      const entry = entries.find((e) => e.id === id);
      if (!entry) return;
      await onUndo(entry.executionId);
      dismiss(id);
    },
    [entries, dismiss, onUndo],
  );

  const undoAll = useCallback(async () => {
    const ids = entries.map((entry) => entry.id);
    if (ids.length === 0) return;
    const executionIds = entries.map((entry) => entry.executionId);
    if (onUndoBatch) {
      await onUndoBatch(executionIds);
    } else {
      await Promise.all(entries.map((entry) => onUndo(entry.executionId)));
    }
    setEntries((current) => current.filter((entry) => !ids.includes(entry.id)));
  }, [entries, onUndo, onUndoBatch]);

  const secondsLeft = useCallback((entry: GraceEntry) => Math.max(0, Math.ceil((entry.deadline - now) / 1000)), [now]);

  return { entries, enqueue, dismiss, undo, undoAll, secondsLeft };
}

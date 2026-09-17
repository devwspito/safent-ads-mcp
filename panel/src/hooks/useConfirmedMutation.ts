import { useEffect, useRef, useState } from "react";
import { confirmationChallengeFrom } from "@/utils/apiError";

interface Options<V, D> {
  scopeKey: string;
  mutate: (variables: V & { confirmationToken?: string }) => Promise<D>;
  summarize: (variables: Readonly<V>) => string[];
  onSuccess?: (data: D, variables: V) => void;
}

function freeze<T>(value: T): T {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

/** Preparation is read-only (428); ONLY an explicit click sends the exact
 * frozen snapshot with its one-shot proof. No automatic mutation retries. */
export function useConfirmedMutation<V extends object, D>(options: Options<V, D>) {
  type Pending = { variables: V; summary: string[]; token: string | null; expires: number;
    scope: string; mutate: Options<V, D>["mutate"]; onSuccess: Options<V, D>["onSuccess"] };
  const pending = useRef<Pending | null>(null);
  const generation = useRef(0);
  const busy = useRef(false);
  const mounted = useRef(true);
  const scope = useRef(options.scopeKey);
  scope.current = options.scopeKey;
  const [view, setView] = useState<Pending | null>(null);
  const [isSubmitting, setSubmitting] = useState(false);
  const [errorMessage, setError] = useState<string | null>(null);

  useEffect(() => {
    // Keep the ref object, not its numeric value: cleanup must invalidate the
    // latest in-flight generation, including work started after this mount.
    const lifecycleGeneration = generation;
    mounted.current = true;
    return () => { mounted.current = false; lifecycleGeneration.current++; pending.current = null; };
  }, []);
  useEffect(() => {
    generation.current++;
    pending.current = null;
    busy.current = false;
    setView(null); setError(null); setSubmitting(false);
  }, [options.scopeKey]);

  async function prepare(variables: V) {
    if (busy.current || pending.current) return;
    const snapshot = freeze(structuredClone(variables));
    const action: Pending = { variables: snapshot, summary: options.summarize(snapshot),
      scope: options.scopeKey, token: null, expires: 0, mutate: options.mutate, onSuccess: options.onSuccess };
    const id = ++generation.current;
    pending.current = action; busy.current = true;
    setView(action); setError(null); setSubmitting(true);
    const current = () => mounted.current && id === generation.current && scope.current === action.scope;
    try {
      await action.mutate({ ...snapshot });
      if (current()) setError("El servidor no solicitó confirmación. Comprueba el estado; no repetiremos la acción.");
    } catch (error) {
      if (!current()) return;
      const challenge = confirmationChallengeFrom(error);
      if (challenge) {
        const ready = { ...action, token: challenge.token, expires: challenge.expiresAt };
        pending.current = ready; setView(ready);
      } else {
        setError("No se pudo preparar la confirmación. Cierra este diálogo y comprueba tu sesión antes de volver a intentarlo.");
      }
    } finally {
      if (current()) { busy.current = false; setSubmitting(false); }
    }
  }

  async function confirm() {
    const action = pending.current;
    if (!action?.token || busy.current || scope.current !== action.scope) return;
    if (Date.now() >= action.expires) {
      pending.current = { ...action, token: null }; setView(pending.current);
      setError("La confirmación ha caducado. Cierra y revisa de nuevo la acción."); return;
    }
    const id = generation.current;
    const token = action.token;
    pending.current = { ...action, token: null }; setView(pending.current);
    busy.current = true; setSubmitting(true); setError(null);
    const current = () => mounted.current && id === generation.current && scope.current === action.scope;
    try {
      const data = await action.mutate({ ...action.variables, confirmationToken: token });
      if (current()) { pending.current = null; setView(null); action.onSuccess?.(data, action.variables); }
    } catch {
      if (current()) setError("No se pudo verificar el resultado. Comprueba el estado antes de iniciar otra acción; esta confirmación no se repetirá.");
    } finally {
      if (current()) { busy.current = false; setSubmitting(false); }
    }
  }

  function cancel() {
    if (busy.current) return;
    generation.current++; pending.current = null; setView(null); setError(null);
  }
  return { isPromptOpen: view !== null, isSubmitting, errorMessage, summary: view?.summary ?? [],
    canConfirm: Boolean(view?.token) && !isSubmitting, start: (v: V) => void prepare(v),
    confirm: () => void confirm(), cancel };
}

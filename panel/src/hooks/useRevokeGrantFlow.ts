import { useCallback, useEffect, useRef, useState } from "react";
import { confirmationChallengeFrom, describeReauthError, reauthMethodsFrom } from "@/utils/apiError";
import { useFreshIdentification } from "./useFreshIdentification";

interface RevokeVariables {
  grantId: string;
}

type Revoke = (variables: RevokeVariables & { reauthToken?: string; confirmationToken?: string }) => Promise<unknown>;

interface UseRevokeGrantFlowOptions {
  /** La `mutateAsync` de `useRevokeMcpOauthGrant` — sin cabecera, con `X-Reauth-Token` o con
   * `X-Action-Confirmation`, según la etapa. */
  revoke: Revoke;
  federatedLoginAvailable: boolean;
  onRevoked: () => void;
}

interface PendingConfirmation {
  variables: RevokeVariables;
  token: string | null;
  expiresAt: number;
}

/** El 401/428 de `attemptRevoke` ya distingue "falta presencia" (rethrown, lo procesa
 * `useFreshIdentification`) de "presencia probada, falta confirmar" — este último trae el reto
 * del 428 consigo, para no tener que volver a pedirlo con una llamada aparte. */
type RevokeAttempt = { revoked: true } | { revoked: false; token: string; expiresAt: number };

function freeze<T>(value: T): T {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

/**
 * `POST /mcp-oauth/grants/{id}/revoke` (contracts/federated-login.md §2): dos comprobaciones
 * encadenadas, no una — identificación fresca (401, delegada a `useFreshIdentification`, sin
 * reinventar esa máquina) y, solo con eso resuelto, la revisión de la acción exacta (428). La
 * segunda etapa copia las guardas de `useConfirmedMutation` (instantánea congelada, token de un
 * solo uso anulado ANTES de enviarlo, generación/montaje para descartar respuestas obsoletas)
 * porque no se pueden reutilizar tal cual: si la identificación fresca caduca ENTRE los dos
 * diálogos (la ventana de 5 min es la misma para TOTP y Google, T060), la confirmación final
 * vuelve a fallar con 401 — y eso no es un error de confirmación cualquiera, es la señal de
 * volver a la primera prueba de presencia, no de enseñar "no se pudo verificar el resultado".
 *
 * El 428 de la llamada que prueba la presencia (`attemptRevoke`) ES el que se confirma — no se
 * repite esa misma llamada sin cabeceras una segunda vez solo para "pedir de nuevo" el token: eso
 * son dos viajes de más por cada revocación y un token de un solo uso que nadie llega a usar.
 *
 * Nota transitoria: hasta que el backend despliegue T060, un servidor pre-T060 puede responder
 * 204 directamente a la primera llamada (sin exigir el 428) — se trata como éxito, no como error,
 * porque es el comportamiento correcto de esa versión del servidor, no una respuesta corrupta.
 */
export function useRevokeGrantFlow({ revoke, federatedLoginAvailable, onRevoked }: UseRevokeGrantFlowOptions) {
  const generation = useRef(0);
  const busy = useRef(false);
  const mounted = useRef(true);
  const pending = useRef<PendingConfirmation | null>(null);
  const [confirmView, setConfirmView] = useState<PendingConfirmation | null>(null);
  const [confirmSubmitting, setConfirmSubmitting] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  useEffect(() => {
    // Keep the ref object, not its numeric value: cleanup must invalidate the latest in-flight
    // generation, including work started after this mount (same pattern as useConfirmedMutation).
    const lifecycleGeneration = generation;
    mounted.current = true;
    return () => {
      mounted.current = false;
      lifecycleGeneration.current++;
      pending.current = null;
    };
  }, []);

  const attemptRevoke = useCallback(
    async (variables: RevokeVariables, reauthToken?: string): Promise<RevokeAttempt> => {
      try {
        await revoke({ ...variables, reauthToken });
        return { revoked: true };
      } catch (error) {
        const challenge = confirmationChallengeFrom(error);
        if (challenge) return { revoked: false, token: challenge.token, expiresAt: challenge.expiresAt };
        throw error;
      }
    },
    [revoke],
  );

  const presence = useFreshIdentification<RevokeVariables, RevokeAttempt>({
    federatedLoginAvailable,
    mutate: attemptRevoke,
    onSuccess: (result, variables) => {
      if (result.revoked) {
        onRevoked();
        return;
      }
      if (!mounted.current) return;
      // El 428 ya viene en `result` (de la misma llamada que probó la presencia): nada que
      // volver a pedir, solo abrir la confirmación con la instantánea congelada de esa petición.
      generation.current++;
      const ready = { variables: freeze({ ...variables }), token: result.token, expiresAt: result.expiresAt };
      pending.current = ready;
      setConfirmError(null);
      setConfirmView(ready);
    },
  });

  async function confirm() {
    const action = pending.current;
    if (!action?.token || busy.current) return;
    if (Date.now() >= action.expiresAt) {
      pending.current = { ...action, token: null };
      setConfirmView(pending.current);
      setConfirmError("La confirmación ha caducado. Cierra y vuelve a intentarlo.");
      return;
    }
    const id = generation.current;
    const token = action.token;
    // El token se anula ANTES de enviarlo: un segundo clic mientras la petición está en vuelo no
    // puede reenviarlo (sería un doble uso), solo `cancel()` puede seguir cerrando el diálogo.
    pending.current = { ...action, token: null };
    setConfirmView(pending.current);
    busy.current = true;
    setConfirmSubmitting(true);
    setConfirmError(null);
    const current = () => mounted.current && id === generation.current;
    try {
      await revoke({ ...action.variables, confirmationToken: token });
      if (current()) {
        pending.current = null;
        setConfirmView(null);
        onRevoked();
      }
    } catch (error) {
      if (!current()) return;
      if (reauthMethodsFrom(error).length > 0) {
        // La identificación fresca caducó entre los dos diálogos: no es un fallo de
        // confirmación, es que hay que volver a probar presencia desde el principio.
        pending.current = null;
        setConfirmView(null);
        presence.start(action.variables);
      } else {
        setConfirmError(describeReauthError(error));
      }
    } finally {
      if (current()) {
        busy.current = false;
        setConfirmSubmitting(false);
      }
    }
  }

  const cancelConfirmation = useCallback(() => {
    if (busy.current) return;
    generation.current++;
    pending.current = null;
    setConfirmView(null);
    setConfirmError(null);
  }, []);

  return {
    presence,
    isConfirmationOpen: confirmView !== null,
    confirmSubmitting,
    confirmError,
    confirmExpiresAt: confirmView?.expiresAt ?? null,
    confirm: () => void confirm(),
    cancelConfirmation,
  };
}

import { useCallback, useEffect, useRef, useState } from "react";
import type { HardCapsUpdate, HardCapsView } from "@/api/schemas/hardCaps";
import { confirmationChallengeFrom, reauthMethodsFrom } from "@/utils/apiError";
import { describeHardCapsError } from "@/utils/hardCaps";
import { useFreshIdentification } from "./useFreshIdentification";

/** Las dos escrituras de la superficie de topes: fijar los tres importes, o retirar el del panel. */
export type HardCapsAction = { kind: "set"; caps: HardCapsUpdate } | { kind: "withdraw" };

/** Las dos pruebas que puede llevar una escritura, nunca las dos a la vez. */
export interface HardCapsCredentials {
  reauthToken?: string;
  confirmationToken?: string;
}

type Submit = (action: HardCapsAction, credentials: HardCapsCredentials) => Promise<HardCapsView>;

interface UseHardCapsFlowOptions {
  /** `PUT`/`DELETE` de `/accounts/{id}/hard-caps` — sin cabecera, con `X-Reauth-Token` o con
   * `X-Action-Confirmation`, según la etapa. El cuerpo lo construye siempre igual el llamador. */
  submit: Submit;
  federatedLoginAvailable: boolean;
  onApplied: (view: HardCapsView) => void;
}

interface PendingConfirmation {
  action: HardCapsAction;
  token: string | null;
  expiresAt: number;
}

/** Lo que devuelve un intento: aplicado, pendiente de confirmar, o fallado con su mensaje ya puesto. */
type Attempt =
  | { status: "applied"; view: HardCapsView }
  | { status: "confirm"; token: string; expiresAt: number }
  | { status: "failed" };

function freeze<T>(value: T): T {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

/**
 * `PUT`/`DELETE /api/v1/accounts/{platform_account_id}/hard-caps` (spec 008 §5,
 * `contracts/hard-caps.openapi.yaml`): dos comprobaciones encadenadas, y en ese orden.
 *
 * 1. **Identificación fresca** (401 `REAUTH_REQUIRED`) solo cuando el cambio SUBE — subir un
 *    importe, fijar el primer tope de una cuenta que hoy deniega el 100 % de las escrituras, o
 *    retirar un tope del panel que destapa una entrada de fichero mayor. Bajar nunca la pide.
 *    La resuelve `useFreshIdentification`, sin reinventar esa máquina.
 * 2. **Confirmación de la acción exacta** (428 `CONFIRMATION_REQUIRED`), siempre. El 428 llega en
 *    la MISMA llamada que probó la presencia: no se repite la petición solo para volver a pedir
 *    un token de un solo uso que nadie llegaría a usar.
 *
 * El reenvío confirmado repite el cuerpo byte a byte y va **sin** `X-Reauth-Token`: la prueba de
 * confirmación está ligada a método, ruta y cuerpo exactos, y el código TOTP ya se quemó.
 *
 * Los rechazos que no son ni presencia ni confirmación (409 del sobre, 400, 503) no se dejan caer
 * en el traductor genérico de errores del panel: cada código de esta superficie tiene su frase
 * (`describeHardCapsError`), porque «alguien más lo cambió mientras tanto» no explica un sobre
 * superado.
 */
export function useHardCapsFlow({ submit, federatedLoginAvailable, onApplied }: UseHardCapsFlowOptions) {
  const generation = useRef(0);
  const busy = useRef(false);
  const mounted = useRef(true);
  const pending = useRef<PendingConfirmation | null>(null);
  const [confirmView, setConfirmView] = useState<PendingConfirmation | null>(null);
  const [confirmSubmitting, setConfirmSubmitting] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    // Se conserva el objeto ref, no su número: la limpieza invalida la última generación en
    // vuelo, incluida la que empezó después de este montaje (igual que `useConfirmedMutation`).
    const lifecycleGeneration = generation;
    mounted.current = true;
    return () => {
      mounted.current = false;
      lifecycleGeneration.current++;
      pending.current = null;
    };
  }, []);

  const attempt = useCallback(
    async (action: HardCapsAction, reauthToken?: string): Promise<Attempt> => {
      setSubmitError(null);
      try {
        const view = await submit(action, { reauthToken });
        return { status: "applied", view };
      } catch (error) {
        const challenge = confirmationChallengeFrom(error);
        if (challenge) return { status: "confirm", token: challenge.token, expiresAt: challenge.expiresAt };
        // Solo la falta de presencia sigue subiendo: es lo único que `useFreshIdentification`
        // sabe resolver. Lo demás se traduce aquí, donde se conocen los códigos de esta API.
        if (reauthMethodsFrom(error).length > 0) throw error;
        if (mounted.current) setSubmitError(describeHardCapsError(error));
        return { status: "failed" };
      }
    },
    [submit],
  );

  const presence = useFreshIdentification<HardCapsAction, Attempt>({
    federatedLoginAvailable,
    mutate: attempt,
    onSuccess: (result, action) => {
      if (result.status === "applied") {
        onApplied(result.view);
        return;
      }
      if (result.status === "failed" || !mounted.current) return;
      generation.current++;
      const ready = { action: freeze({ ...action }), token: result.token, expiresAt: result.expiresAt };
      pending.current = ready;
      setConfirmError(null);
      setConfirmView(ready);
    },
  });

  async function confirm() {
    const ready = pending.current;
    if (!ready?.token || busy.current) return;
    if (Date.now() >= ready.expiresAt) {
      pending.current = { ...ready, token: null };
      setConfirmView(pending.current);
      setConfirmError("La confirmación ha caducado. Cierra y vuelve a intentarlo.");
      return;
    }
    const id = generation.current;
    const token = ready.token;
    // El token se anula ANTES de enviarlo: un segundo clic con la petición en vuelo no puede
    // reenviarlo — sería un doble uso, y el servidor ya lo cuenta como gastado.
    pending.current = { ...ready, token: null };
    setConfirmView(pending.current);
    busy.current = true;
    setConfirmSubmitting(true);
    setConfirmError(null);
    const current = () => mounted.current && id === generation.current;
    try {
      const view = await submit(ready.action, { confirmationToken: token });
      if (current()) {
        pending.current = null;
        setConfirmView(null);
        onApplied(view);
      }
    } catch (error) {
      if (!current()) return;
      if (reauthMethodsFrom(error).length > 0) {
        // La identificación fresca caducó entre los dos diálogos: se vuelve a la primera prueba
        // de presencia, no es un fallo de confirmación.
        pending.current = null;
        setConfirmView(null);
        presence.start(ready.action);
      } else {
        setConfirmError(describeHardCapsError(error));
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
    start: (action: HardCapsAction) => {
      setSubmitError(null);
      presence.start(action);
    },
    pendingAction: confirmView?.action ?? null,
    isConfirmationOpen: confirmView !== null,
    confirmSubmitting,
    confirmError,
    /** Rechazo que no abre ningún diálogo (sobre superado, bróker caído): se pinta en la sección. */
    submitError,
    confirm: () => void confirm(),
    cancelConfirmation,
  };
}

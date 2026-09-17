import { useState } from "react";
import { useStartFederatedLogin } from "@/api/queries/auth";
import { describeApiError, describeReauthError, reauthMethodsFrom, type ReauthMethod } from "@/utils/apiError";
import { navigateTo } from "@/utils/navigation";

interface UseFreshIdentificationOptions<TVariables, TData> {
  /** La `mutateAsync` de una `useMutation` existente — TOTP fresco va en `reauthToken`. */
  mutate: (variables: TVariables, reauthToken?: string) => Promise<TData>;
  onSuccess?: (data: TData, variables: TVariables) => void;
  /** Transacción de consentimiento a la que volver tras Google (`POST /auth/federated/start`). */
  txnId?: string | null;
  /**
   * `useMe().federated_login_available` (contracts/federated-login.md §2): red de seguridad
   * adicional del cliente — aunque el 401 trajera `methods` con `"federated"`, nunca se ofrece
   * Google si el llamador sabe que el interruptor está apagado. Obligatorio y por defecto-denegar
   * a propósito: el llamador tiene que decidir explícitamente qué vale mientras `useMe()` está
   * cargando, no heredar un `true` implícito que un olvido futuro dejaría sin red de seguridad.
   */
  federatedLoginAvailable: boolean;
}

interface UseFreshIdentificationResult<TVariables> {
  /** `true` mientras el `FreshIdentificationPrompt` está abierto esperando una prueba de presencia. */
  isPromptOpen: boolean;
  /** Vías que este dueño puede usar de verdad — `contracts/federated-login.md` §2. */
  methods: ReauthMethod[];
  isSubmitting: boolean;
  errorMessage: string | null;
  isStartingGoogle: boolean;
  /** Fallo de `POST /auth/federated/start` — antes de llegar a navegar a Google. */
  federatedStartError: string | null;
  /** Intenta la mutación SIN ninguna cabecera. Un 401 `REAUTH_REQUIRED` abre el prompt. */
  start: (variables: TVariables) => void;
  /** Añade el código TOTP a la acción guardada en `start` y reintenta. */
  confirm: (code: string) => void;
  /** Arranca el salto a Google — siempre consecuencia de un clic, nunca automática (FR-114). */
  confirmWithGoogle: () => void;
  retryFederatedStart: () => void;
  cancel: () => void;
}

/**
 * Prueba de presencia única para mutaciones sensibles (contracts/federated-login.md §2 y §4,
 * `iam/presentation/fresh_identification.py`): el primer intento sale sin cabecera — el 401 lo
 * produce la dependencia del servidor antes del caso de uso, así que no hay ningún efecto que
 * deshacer. Si el 401 trae `details.methods`, abre `FreshIdentificationPrompt` con la variante
 * que corresponda (TOTP, Google, o ambas); si no trae `details`, es comportamiento legado
 * (spec 002, solo TOTP). Sustituye a `useReauthedMutation`.
 */
export function useFreshIdentification<TVariables, TData>({
  mutate,
  onSuccess,
  txnId = null,
  federatedLoginAvailable,
}: UseFreshIdentificationOptions<TVariables, TData>): UseFreshIdentificationResult<TVariables> {
  const [pendingVariables, setPendingVariables] = useState<{ value: TVariables } | null>(null);
  const [methods, setMethods] = useState<ReauthMethod[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [federatedStartError, setFederatedStartError] = useState<string | null>(null);
  const startFederatedLogin = useStartFederatedLogin();

  async function start(variables: TVariables) {
    setErrorMessage(null);
    setFederatedStartError(null);
    setIsSubmitting(true);
    try {
      const data = await mutate(variables);
      setIsSubmitting(false);
      onSuccess?.(data, variables);
    } catch (error) {
      setIsSubmitting(false);
      const availableMethods = reauthMethodsFrom(error).filter(
        (method) => method !== "federated" || federatedLoginAvailable,
      );
      if (availableMethods.length > 0) {
        setPendingVariables({ value: variables });
        setMethods(availableMethods);
        return;
      }
      setErrorMessage(describeApiError(error));
    }
  }

  async function confirm(code: string) {
    if (pendingVariables === null) return;
    setErrorMessage(null);
    setIsSubmitting(true);
    try {
      const data = await mutate(pendingVariables.value, code);
      const variables = pendingVariables.value;
      setPendingVariables(null);
      setIsSubmitting(false);
      onSuccess?.(data, variables);
    } catch (error) {
      setIsSubmitting(false);
      setErrorMessage(describeReauthError(error));
    }
  }

  async function confirmWithGoogle() {
    setFederatedStartError(null);
    try {
      const { authorization_url } = await startFederatedLogin.mutateAsync(txnId);
      if (!navigateTo(authorization_url)) {
        setFederatedStartError("No se ha podido abrir Google. Vuelve a intentarlo.");
      }
    } catch (error) {
      setFederatedStartError(describeApiError(error));
    }
  }

  function cancel() {
    setPendingVariables(null);
    setMethods([]);
    setErrorMessage(null);
    setFederatedStartError(null);
  }

  return {
    isPromptOpen: pendingVariables !== null,
    methods,
    isSubmitting,
    errorMessage,
    isStartingGoogle: startFederatedLogin.isPending,
    federatedStartError,
    start: (variables: TVariables) => void start(variables),
    confirm: (code: string) => void confirm(code),
    confirmWithGoogle: () => void confirmWithGoogle(),
    retryFederatedStart: () => setFederatedStartError(null),
    cancel,
  };
}

import { ApiRequestError } from "@/api/client";

/**
 * Mensaje llano por clase de error — un único patrón para toda lectura fallida
 * (panel-interaction-spec.md §2: "lenguaje llano y `Reintentar` que repite esa petición").
 * 401 `UNAUTHORIZED` (sesión caducada/ausente) ya dispara la redirección global a `/login`
 * (`AppShell`, `onUnauthorized`); este mensaje solo se ve en el instante entre el fallo y esa
 * redirección. La confirmación humana se resuelve en su propio diálogo.
 */
export function describeApiError(error: unknown): string {
  if (error instanceof ApiRequestError) {
    switch (error.status) {
      case 401:
        return "Tu sesión ha caducado. Vuelve a entrar.";
      case 403:
        return "No tienes permiso para ver esto.";
      case 404:
        return "No hemos encontrado esto. Puede que ya no exista.";
      case 409:
        return "Alguien más lo cambió mientras tanto. Actualiza antes de seguir.";
      case 429:
        return "Demasiadas peticiones seguidas. Espera un momento y reintenta.";
      default:
        if (error.status >= 500) return "El servidor ha tenido un problema. No es nada que hayas hecho.";
        return error.message || "Algo ha fallado en el servidor.";
    }
  }
  if (error instanceof TypeError) {
    return "Sin conexión con el servidor. Comprueba tu red.";
  }
  return "Inténtalo de nuevo en unos segundos.";
}

/**
 * Spec 002 (mcp_oauth): el error de `X-Reauth-Token` que muestra
 * `FreshIdentificationPrompt` -- `REAUTH_REQUIRED` es un código mal escrito o
 * caducado, no una sesión perdida, y decirlo como `describeApiError`
 * ("Tu sesión ha caducado") mandaría a la persona a volver a entrar sin
 * motivo.
 */
export function describeReauthError(error: unknown): string {
  if (error instanceof ApiRequestError) {
    if (error.code === "REAUTH_REQUIRED") {
      return "Código incorrecto o caducado. Escribe el código actual de tu aplicación de autenticación.";
    }
    if (error.status === 429) {
      return "Demasiados intentos. Espera unos minutos y vuelve a intentarlo.";
    }
  }
  return describeApiError(error);
}

export type ReauthMethod = "totp" | "federated";

function isReauthMethod(value: unknown): value is ReauthMethod {
  return value === "totp" || value === "federated";
}

/**
 * `details.methods` de un 401 `REAUTH_REQUIRED` (contracts/federated-login.md §2): las vías
 * que ese dueño puede usar de verdad. `details` ausente es comportamiento legado (spec 002,
 * solo TOTP) — nunca una lista vacía, que dejaría `FreshIdentificationPrompt` sin nada que
 * pintar.
 */
export function reauthMethodsFrom(error: unknown): ReauthMethod[] {
  if (!(error instanceof ApiRequestError) || error.code !== "REAUTH_REQUIRED") return [];
  const methods = error.details?.methods;
  if (Array.isArray(methods) && methods.length > 0 && methods.every(isReauthMethod)) {
    return methods;
  }
  return ["totp"];
}

export interface ConfirmationChallenge {
  token: string;
  expiresAt: number;
}

/** Ventana máxima que se acepta entre "el servidor pidió confirmar" y "caduca" — más allá de
 * esto, `details.expires_at` es sospechoso, no una caducidad real (lane/003). */
export const CONFIRMATION_MAX_TTL_MS = 125_000;

/**
 * `428 CONFIRMATION_REQUIRED` (rest-api.md, lane/003): copia única de esta extracción — la usan
 * `useConfirmedMutation` y `useRevokeGrantFlow` — para que la validación del token (longitud,
 * ventana de caducidad) no diverja entre los dos sitios que la necesitan.
 */
export function confirmationChallengeFrom(error: unknown): ConfirmationChallenge | null {
  if (!(error instanceof ApiRequestError) || error.status !== 428 || error.code !== "CONFIRMATION_REQUIRED") {
    return null;
  }
  const token = error.details?.confirmation_token;
  const expiresAtRaw = error.details?.expires_at;
  const expiresAt = typeof expiresAtRaw === "string" ? Date.parse(expiresAtRaw) : NaN;
  if (typeof token !== "string" || token.length === 0 || token.length > 2048) return null;
  if (!(expiresAt > Date.now() && expiresAt <= Date.now() + CONFIRMATION_MAX_TTL_MS)) return null;
  return { token, expiresAt };
}

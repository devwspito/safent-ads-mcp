/**
 * `contracts/federated-login.md` §1 — las cinco filas estables de `federated_error`, con el
 * texto exacto que ve el dueño. Un único módulo porque el servidor manda el mismo parámetro
 * a dos vueltas distintas: `/login?federated_error=<código>` (entrar) y
 * `/oauth/autorizar?txn=…&federated_error=<código>` (re-identificar, FR-113).
 */
type FederatedErrorCode =
  | "denied"
  | "expired"
  | "provider_unavailable"
  | "identity_mismatch"
  | "owner_already_bound";

const FEDERATED_ERROR_MESSAGES: Record<FederatedErrorCode, string> = {
  denied: "No hemos podido entrar con esa cuenta. Prueba con otra o entra con tu correo y contraseña.",
  expired: "La solicitud ha caducado. Vuelve a intentarlo.",
  provider_unavailable: "Google no responde ahora mismo. Vuelve a intentarlo o entra con tu correo y contraseña.",
  identity_mismatch: "Esa no es la cuenta con la que entraste. Vuelve a intentarlo con la misma.",
  owner_already_bound: "Esta instalación ya tiene dueño. Pide acceso a quien la administra.",
};

const FEDERATED_ERROR_FALLBACK = "No se ha podido entrar con Google. Vuelve a intentarlo.";

/** `code` es un parámetro de la URL, nunca fiable: `Object.hasOwn` evita que una clave heredada
 *  del prototipo (`__proto__`, `constructor`, `toString`…) se cuele como "encontrada". */
export function describeFederatedError(code: string): string {
  return Object.hasOwn(FEDERATED_ERROR_MESSAGES, code)
    ? FEDERATED_ERROR_MESSAGES[code as FederatedErrorCode]
    : FEDERATED_ERROR_FALLBACK;
}

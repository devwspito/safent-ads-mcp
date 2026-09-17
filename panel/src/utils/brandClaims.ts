/** Mismos límites que valida `PUT /brand/claims` en el servidor
 * (`brand/presentation/payloads.py`) — el panel los aplica antes de enviar para dar
 * retroalimentación al momento, el servidor sigue siendo quien decide de verdad. */
export const CLAIM_MIN_LENGTH = 2;
export const CLAIM_MAX_LENGTH = 80;
export const MAX_CLAIMS_PER_LIST = 50;

export function normalizeClaim(raw: string): string {
  return raw.trim();
}

export function claimLengthError(claim: string): string | null {
  if (claim.length >= CLAIM_MIN_LENGTH && claim.length <= CLAIM_MAX_LENGTH) return null;
  return `Debe tener entre ${CLAIM_MIN_LENGTH} y ${CLAIM_MAX_LENGTH} caracteres.`;
}

export function hasCaseInsensitiveMatch(claim: string, list: readonly string[]): boolean {
  const target = claim.toLowerCase();
  return list.some((item) => item.toLowerCase() === target);
}

/** Reclamos permitidos que coinciden (insensible a mayúsculas) con uno prohibido —
 * mismo criterio de coincidencia exacta que `find_allow_forbid_conflicts` en el dominio. */
export function findConflictingClaims(
  claimsAllowlist: readonly string[],
  forbiddenClaims: readonly string[],
): string[] {
  const forbiddenCf = new Set(forbiddenClaims.map((claim) => claim.toLowerCase()));
  return claimsAllowlist.filter((claim) => forbiddenCf.has(claim.toLowerCase()));
}

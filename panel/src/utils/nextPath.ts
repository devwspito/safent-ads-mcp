const DEFAULT_LANDING = "/propuestas";

/**
 * `?next=` (LoginPage; contracts/oauth.md §4) solo puede ser una ruta del propio
 * panel: empieza por una única `/` (nunca `//`, protocolo-relativo) y, resuelta contra el origen
 * actual, sigue en ese origen — así `https://malo.example`/`//malo.example` nunca sacan al
 * propietario del panel.
 */
export function sanitizeNextPath(rawNext: string | null): string {
  if (!rawNext || !rawNext.startsWith("/") || rawNext.startsWith("//")) return DEFAULT_LANDING;
  try {
    const resolved = new URL(rawNext, window.location.origin);
    if (resolved.origin !== window.location.origin) return DEFAULT_LANDING;
    return `${resolved.pathname}${resolved.search}${resolved.hash}`;
  } catch {
    return DEFAULT_LANDING;
  }
}

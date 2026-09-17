/**
 * Aísla `window.location.assign` en un módulo propio: jsdom no deja redefinir
 * `Location.prototype.assign` (no es `configurable`), así que `vi.spyOn(window.location, "assign")`
 * falla en todas las suites — mockear este módulo entero es la costura de prueba.
 *
 * También es dueño de la lista blanca de esquemas: solo navega a `http:`/`https:` (nunca
 * `javascript:`, `data:`, etc.). Devuelve si la navegación se produjo para que el llamador
 * pueda distinguir éxito de un `redirect_to` rechazado.
 */
export function navigateTo(url: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(url, window.location.origin);
  } catch {
    return false;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
  window.location.assign(url);
  return true;
}

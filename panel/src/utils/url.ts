/**
 * Sólo http: y https: son destinos válidos para href/window.open — bloquea javascript:, data:, etc.
 */
export function isSafeUrl(url: string): boolean {
  if (!url) return false;
  try {
    const parsed = new URL(url, window.location.origin);
    return parsed.protocol === "https:" || parsed.protocol === "http:";
  } catch {
    return false;
  }
}

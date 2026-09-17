/**
 * Prefijo con el que el documento actual se sirve (026, contracts/
 * cockpit-read-model.md §5): "" en directo, "/ads" empotrado bajo el
 * puente de sesión de Safent. `composition/app.py::_render_embedded_index_html`
 * inyecta metadata no ejecutable en `index.html`, compatible con la CSP --
 * un único punto de lectura para que el router (`App.tsx`) y el cliente
 * HTTP (`api/client.ts`) nunca se desincronicen sobre qué prefijo usar.
 */
export function getAdsBasePath(): string {
  if (typeof document === "undefined") return "";
  const entries = document.querySelectorAll<HTMLMetaElement>('meta[name="safent-ads-base-path"]');
  if (entries.length !== 1) return "";
  return entries[0]?.content === "/ads" ? "/ads" : "";
}

export function isAdsEmbedded(): boolean {
  return getAdsBasePath() === "/ads";
}

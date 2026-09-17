/**
 * Identidad de la instancia (contracts/instance-identity.d.ts): como se llama ESTE
 * servidor para quien lo autoriza, no la marca del negocio (eso es `ADS_BRAND_NAME`,
 * que el panel no necesita). `composition/app.py::_render_embedded_index_html` inyecta
 * dos `<meta>` no ejecutables (mismo canal que ya usa `safent-ads-base-path`, ver
 * `basePath.ts`) — único punto de lectura, ningún otro componente lee el DOM por su
 * cuenta.
 *
 * Deliberadamente NO lee `window.__ADS_INSTANCE__` ni ningún otro global: nada lo
 * asigna nunca (el servidor solo inyecta `<meta>`, CSP prohíbe el script inline que
 * haría falta para fijarlo), así que sería una superficie de suplantación muerta —
 * cualquier script de terceros que corriera en el origen del panel podría escribir
 * ese global ANTES de que el bootstrap lo lea y hacer que la pantalla de
 * consentimiento (R-6 del threat-model) muestre un nombre de instancia falso.
 *
 * Defectos cuando la inyección no está (`vite dev`, tests): `{ name: "Ads MCP",
 * panelHost: location.host }`. Nunca el nombre de un cliente.
 */
export interface InstanceIdentity {
  readonly name: string;
  readonly panelHost: string;
}

const DEFAULT_NAME = "Ads MCP";

function metaContent(name: string): string | null {
  if (typeof document === "undefined") return null;
  const entries = document.querySelectorAll<HTMLMetaElement>(`meta[name="${name}"]`);
  return entries.length === 1 ? (entries[0]?.content ?? null) : null;
}

function defaultPanelHost(): string {
  return typeof location === "undefined" ? "" : location.host;
}

export function instanceIdentity(): InstanceIdentity {
  return {
    name: metaContent("ads-instance-name") || DEFAULT_NAME,
    panelHost: metaContent("ads-panel-host") || defaultPanelHost(),
  };
}

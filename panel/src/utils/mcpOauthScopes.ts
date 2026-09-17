import type { McpOauthConsentScope, McpOauthScopeName } from "@/api/schemas/mcpOauth";

/**
 * Copia en palabras llanas por alcance MCP, acordada para la pantalla de consentimiento y la
 * lista de "Agentes conectados" — mismo texto en ambas superficies (una sola fuente). El
 * ejemplo de `contracts/oauth.md` §5 trae su propio `label` por alcance; se usa como reserva
 * para un alcance futuro que el panel aún no conozca, nunca para `ads:read`/`ads:propose`.
 */
const SCOPE_COPY: Record<McpOauthScopeName, string> = {
  "ads:read": "ver negocios, cuentas, campañas y resultados",
  "ads:propose": "proponer campañas y cambios, que tú apruebas",
};

/** Para la pantalla de consentimiento (`scopes: [{name, label}]`). */
export function describeConsentScope(scope: McpOauthConsentScope): string {
  return SCOPE_COPY[scope.name] ?? scope.label;
}

/** Para "Agentes conectados" (`scopes: string[]`, sin `label` del servidor). */
export function describeGrantScope(scope: McpOauthScopeName): string {
  return SCOPE_COPY[scope];
}

/** `contracts/oauth.md` §5 y `tasks.md` T015b — consentimiento y "Agentes conectados". */
import type { McpOauthConsent, McpOauthConsentActionResponse, McpOauthGrant } from "@/api/schemas/mcpOauth";

export const MOCK_CONSENT_TXN_ID = "txn_demo_claude_code";

const MOCK_CLAUDE_CLIENT_ID = "8f14e45f-ceea-467e-bd3d-bf24ea7c9a10";
const MOCK_CODEX_CLIENT_ID = "3af9f8b0-3e34-4a53-9d4a-6a9b6b7a9abc";

interface ConsentRecord extends McpOauthConsent {
  state: "pending" | "resolved";
}

function defaultConsents(): Record<string, ConsentRecord> {
  return {
    [MOCK_CONSENT_TXN_ID]: {
      txn_id: MOCK_CONSENT_TXN_ID,
      client_id: MOCK_CLAUDE_CLIENT_ID,
      client_name: "Claude Code",
      redirect_host: "127.0.0.1:54321",
      scopes: [
        { name: "ads:read", label: "Leer tu cartera, señales y registro" },
        { name: "ads:propose", label: "Crear propuestas (siguen necesitando tu aprobación)" },
      ],
      expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
      state: "pending",
    },
  };
}

function defaultGrants(): McpOauthGrant[] {
  return [
    {
      grant_id: "grant_claude_code",
      client_id: MOCK_CLAUDE_CLIENT_ID,
      client_name: "Claude Code",
      redirect_host: "127.0.0.1:54321",
      scopes: ["ads:read", "ads:propose"],
      created_at: new Date(Date.now() - 7 * 86_400_000).toISOString(),
      expires_at: new Date(Date.now() + 23 * 86_400_000).toISOString(),
      last_used_at: new Date(Date.now() - 3_600_000).toISOString(),
    },
    {
      grant_id: "grant_codex",
      client_id: MOCK_CODEX_CLIENT_ID,
      client_name: "Codex",
      redirect_host: "localhost:1455",
      scopes: ["ads:read"],
      created_at: new Date(Date.now() - 86_400_000).toISOString(),
      expires_at: new Date(Date.now() + 29 * 86_400_000).toISOString(),
      last_used_at: null,
    },
  ];
}

/** Grant sin refresh token activo (`expires_at: null` en `application/list_grants.py`). */
export const MOCK_GRANT_WITHOUT_EXPIRY: McpOauthGrant = {
  grant_id: "grant_sin_caducidad",
  client_id: MOCK_CODEX_CLIENT_ID,
  client_name: "Codex sin refresco",
  redirect_host: "localhost:1455",
  scopes: ["ads:read"],
  created_at: new Date(Date.now() - 2 * 86_400_000).toISOString(),
  expires_at: null,
  last_used_at: null,
};

let consents = defaultConsents();
let grants = defaultGrants();

/**
 * Identificación fresca (contracts/federated-login.md §2) — dos formas, no una:
 * - **Federada**: de sesión, reutilizable en CUALQUIER mutación sensible dentro de la ventana
 *   (una identificación con Google vale para revocar, aprobar, lo que sea). Una revocación que
 *   se completa la consume — el servidor la borra tras el 204, no queda "gratis" para lo
 *   siguiente.
 * - **TOTP**: la fila de confirmación es POR ACCIÓN (mismo método + ruta — aquí, el mismo
 *   `grant_id` para revocar o el mismo `txn_id` para aprobar), nunca de sesión. El código que
 *   prueba presencia para revocar el grant A no sirve para aprobar un consentimiento, ni para
 *   revocar el grant B — cada acción exige su propio código, aunque las dos hayan pasado dentro
 *   de la misma ventana de 5 minutos.
 */
const FRESHNESS_WINDOW_MS = 5 * 60_000;
let federatedFreshUntil = 0;
const totpFreshByAction = new Map<string, number>();

/** Solo para tests: simula que el dueño acaba de identificarse con Google (de sesión). */
export function markMockFederatedPresenceFresh() {
  federatedFreshUntil = Date.now() + FRESHNESS_WINDOW_MS;
}

export function isMockFederatedPresenceFresh(): boolean {
  return Date.now() < federatedFreshUntil;
}

/** Una revocación que se completa consume la identificación federada (la borra el servidor). */
export function clearMockFederatedPresence() {
  federatedFreshUntil = 0;
}

/** `actionKey` identifica la acción exacta (p.ej. `revoke:${grantId}`, `approve:${txnId}`). */
export function markMockTotpPresenceFresh(actionKey: string) {
  totpFreshByAction.set(actionKey, Date.now() + FRESHNESS_WINDOW_MS);
}

export function isMockTotpPresenceFresh(actionKey: string): boolean {
  const expiresAt = totpFreshByAction.get(actionKey);
  return typeof expiresAt === "number" && Date.now() < expiresAt;
}

export function resetMcpOauthFixtures() {
  consents = defaultConsents();
  grants = defaultGrants();
  federatedFreshUntil = 0;
  totpFreshByAction.clear();
}

export function findMockConsent(txnId: string): McpOauthConsent | null {
  const record = consents[txnId];
  if (!record || record.state !== "pending") return null;
  const { txn_id, client_id, client_name, redirect_host, scopes, expires_at } = record;
  return { txn_id, client_id, client_name, redirect_host, scopes, expires_at };
}

function redirectFor(consent: ConsentRecord, outcome: "approve" | "deny"): McpOauthConsentActionResponse {
  const url = new URL(`http://${consent.redirect_host}/callback`);
  url.searchParams.set("state", "mock_state");
  if (outcome === "approve") {
    url.searchParams.set("code", `mock_code_${consent.txn_id}`);
  } else {
    url.searchParams.set("error", "access_denied");
  }
  return { redirect_to: url.toString() };
}

export function approveMockConsent(txnId: string): McpOauthConsentActionResponse | null {
  const record = consents[txnId];
  if (!record || record.state !== "pending") return null;
  record.state = "resolved";
  grants = [
    {
      grant_id: `grant_${txnId}`,
      client_id: record.client_id,
      client_name: record.client_name,
      redirect_host: record.redirect_host,
      scopes: record.scopes.map((scope) => scope.name),
      created_at: new Date().toISOString(),
      expires_at: new Date(Date.now() + 30 * 86_400_000).toISOString(),
      last_used_at: null,
    },
    ...grants,
  ];
  return redirectFor(record, "approve");
}

export function denyMockConsent(txnId: string): McpOauthConsentActionResponse | null {
  const record = consents[txnId];
  if (!record || record.state !== "pending") return null;
  record.state = "resolved";
  return redirectFor(record, "deny");
}

export function listMockGrants(): McpOauthGrant[] {
  return grants;
}

export function revokeMockGrant(grantId: string) {
  grants = grants.filter((grant) => grant.grant_id !== grantId);
}

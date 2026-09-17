/** `contracts/rest-api.md` §Conexiones → Cloudflare (lane 006-cloudflare-ui). */
import type { CloudflareConnectionStatus } from "@/api/schemas/cloudflare";

const PROFILE_TOKENS_URL = "https://dash.cloudflare.com/profile/api-tokens";
const REQUIRED_PERMISSIONS = ["Zone.Read", "DNS.Edit"];

function disconnected(): CloudflareConnectionStatus {
  return {
    connected: false,
    account_id: null,
    zones: [],
    create_token_url: PROFILE_TOKENS_URL,
    required_permissions: REQUIRED_PERMISSIONS,
    connected_at: null,
  };
}

// Empieza DESCONECTADO a propósito (a diferencia de `platformApps.ts`, que
// arranca configurado): esta es la tarjeta nueva, así que su demo por
// defecto es la primera vez que el propietario la ve.
let state: CloudflareConnectionStatus = disconnected();

export function resetCloudflareFixtures() {
  state = disconnected();
}

export function getCloudflareConnection() {
  return state;
}

export function connectCloudflareToken(input: { account_id?: string }) {
  const accountId = input.account_id ?? null;
  state = {
    connected: true,
    account_id: accountId,
    zones: ["example.com", "example.net"],
    create_token_url: accountId ? `https://dash.cloudflare.com/${accountId}/api-tokens` : PROFILE_TOKENS_URL,
    required_permissions: REQUIRED_PERMISSIONS,
    connected_at: new Date().toISOString(),
  };
  return state;
}

export function disconnectCloudflareToken() {
  state = disconnected();
}

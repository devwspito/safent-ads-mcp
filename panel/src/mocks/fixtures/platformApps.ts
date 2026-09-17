/** `contracts/rest-api.md` §Conexiones → Credenciales de desarrollador. */
export type MockPlatform = "google" | "meta";

interface PlatformAppRecord {
  platform: MockPlatform;
  configured: boolean;
  client_id_masked: string | null;
  login_customer_id_masked: string | null;
  redirect_uri: string;
  updated_at: string | null;
}

function redirectUriFor(platform: MockPlatform): string {
  return `https://ads.example.ts.net/api/v1/platform-accounts/${platform}/reconnect/callback`;
}

// Ambas plataformas empiezan CONFIGURADAS a propósito: `ConexionesPage.test.tsx`
// prueba "Conectar" sin pasar antes por esta tarjeta — mismo criterio que el
// resto de fixtures del panel (estado de demo ya listo), `resetPlatformAppsFixtures`
// es lo que usan los tests que sí quieren partir de "sin configurar".
function defaultState(): Record<MockPlatform, PlatformAppRecord> {
  return {
    google: {
      platform: "google",
      configured: true,
      client_id_masked: "****.com",
      login_customer_id_masked: "****7890",
      redirect_uri: redirectUriFor("google"),
      updated_at: new Date().toISOString(),
    },
    meta: {
      platform: "meta",
      configured: true,
      client_id_masked: "****3210",
      login_customer_id_masked: null,
      redirect_uri: redirectUriFor("meta"),
      updated_at: new Date().toISOString(),
    },
  };
}

let state = defaultState();

export function resetPlatformAppsFixtures() {
  state = defaultState();
}

export function listPlatformApps() {
  return { items: [state.google, state.meta] };
}

export function setGoogleAppCredentials(input: { client_id: string; login_customer_id?: string }) {
  state.google = {
    platform: "google",
    configured: true,
    client_id_masked: `****${input.client_id.slice(-4)}`,
    login_customer_id_masked: input.login_customer_id ? `****${input.login_customer_id.slice(-4)}` : null,
    redirect_uri: redirectUriFor("google"),
    updated_at: new Date().toISOString(),
  };
  return state.google;
}

export function setMetaAppCredentials(input: { app_id: string }) {
  state.meta = {
    platform: "meta",
    configured: true,
    client_id_masked: `****${input.app_id.slice(-4)}`,
    login_customer_id_masked: null,
    redirect_uri: redirectUriFor("meta"),
    updated_at: new Date().toISOString(),
  };
  return state.meta;
}

export function deletePlatformAppCredentials(platform: MockPlatform) {
  state[platform] = {
    platform,
    configured: false,
    client_id_masked: null,
    login_customer_id_masked: null,
    redirect_uri: redirectUriFor(platform),
    updated_at: null,
  };
}

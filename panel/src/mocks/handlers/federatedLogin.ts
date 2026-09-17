import { http, HttpResponse } from "msw";
import { API_BASE } from "./apiBase";

export const MOCK_FEDERATED_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth?mock=1";

/**
 * `contracts/federated-login.md` §1 — apagado por defecto en los tests: cada suite que
 * necesite el botón «Entrar con Google» debe llamar a `setMockFederatedLoginAvailable(true)`
 * explícitamente, igual que el backend real con `ADS_FEDERATED_LOGIN_ENABLED=false`.
 */
let federatedLoginAvailable = false;

function notFound() {
  return HttpResponse.json(
    { error: { code: "NOT_FOUND", message: "No encontrado." } },
    { status: 404 },
  );
}

export const federatedLoginHandlers = [
  http.get(`${API_BASE}/auth/federated/status`, () => {
    if (!federatedLoginAvailable) return notFound();
    return HttpResponse.json({ available: true }, { headers: { "Cache-Control": "no-store" } });
  }),

  http.post(`${API_BASE}/auth/federated/start`, async ({ request }) => {
    if (!federatedLoginAvailable) return notFound();
    const body = (await request.json().catch(() => ({}))) as { txn_id?: string | null };
    return HttpResponse.json({
      authorization_url: body.txn_id
        ? `${MOCK_FEDERATED_AUTHORIZATION_URL}&txn_id=${body.txn_id}`
        : MOCK_FEDERATED_AUTHORIZATION_URL,
      expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
    });
  }),
];

/** Sólo para tests: activa o apaga el login federado como haría el interruptor del servidor. */
export function setMockFederatedLoginAvailable(available: boolean) {
  federatedLoginAvailable = available;
}

/** `GET /auth/me`'s `federated_login_available` (contracts/federated-login.md §2) — mismo interruptor. */
export function isMockFederatedLoginAvailable(): boolean {
  return federatedLoginAvailable;
}

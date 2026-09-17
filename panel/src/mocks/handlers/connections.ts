import { http, HttpResponse } from "msw";
import { requireMockConfirmation } from "./actionConfirmation";
import {
  getTelegramPairing,
  listPlatformAccounts,
  reconnectStatus,
  registerMetaSystemUserToken,
  revokePlatformAccount,
  startReconnect,
  startTelegramPairing,
  unpairTelegram,
} from "../fixtures/connections";
import { API_BASE } from "./apiBase";

const REVOKE_ERROR_MESSAGES: Record<string, string> = {
  NOT_FOUND: "No se encontró la cuenta.",
  CREDENTIAL_ALREADY_REVOKED: "La credencial ya estaba revocada.",
};


export const connectionsHandlers = [
  http.get(`${API_BASE}/platform-accounts`, () => HttpResponse.json(listPlatformAccounts())),

  // `{provider}` es el código de plataforma (`google`|`meta`), no un `platform_account_id`
  // (rest-api.md §Conexiones): el propietario autoriza, no una cuenta concreta.
  http.post(`${API_BASE}/platform-accounts/:provider/reconnect/start`, ({ params }) => {
    const provider = params.provider === "meta" ? "meta" : "google";
    return HttpResponse.json(startReconnect(provider), { status: 201 });
  }),

  http.get(`${API_BASE}/platform-accounts/:provider/reconnect/status`, ({ request }) => {
    const url = new URL(request.url);
    const sessionId = url.searchParams.get("session_id") ?? "";
    return HttpResponse.json(reconnectStatus(sessionId));
  }),

  http.post(`${API_BASE}/platform-accounts/meta/system-user-token`, async ({ request }) => {
    const body = (await request.json()) as { token?: string };
    if (!body.token?.trim()) {
      return HttpResponse.json(
        { error: { code: "VALIDATION_ERROR", message: "Falta el token.", details: {} } },
        { status: 422 },
      );
    }
    return HttpResponse.json(registerMetaSystemUserToken(), { status: 201 });
  }),

  // `{id}` aquí sí es un `platform_account_id` real (`google:123`) — revoke es la excepción
  // a la regla de arriba (rest-api.md §Conexiones).
  http.post(`${API_BASE}/platform-accounts/:id/revoke`, ({ params }) => {
    const result = revokePlatformAccount(String(params.id));
    if (!result.ok) {
      const status = result.code === "NOT_FOUND" ? 404 : 409;
      return HttpResponse.json({ error: { code: result.code, message: REVOKE_ERROR_MESSAGES[result.code], details: {} } }, { status });
    }
    return new HttpResponse(null, { status: 204 });
  }),

  http.get(`${API_BASE}/telegram/pairing`, () => HttpResponse.json(getTelegramPairing())),

  http.post(`${API_BASE}/telegram/pairing/start`, async ({ request }) => {
    const confirmation = await requireMockConfirmation(request);
    if (confirmation) return confirmation;
    return HttpResponse.json(startTelegramPairing(), { status: 201 });
  }),

  http.delete(`${API_BASE}/telegram/pairing`, async ({ request }) => {
    const body = (await request.json()) as { typed_confirmation: string };
    if (body.typed_confirmation?.trim().toUpperCase() !== "DESEMPAREJAR") {
      return HttpResponse.json(
        { error: { code: "TYPED_CONFIRMATION_REQUIRED", message: "Escribe DESEMPAREJAR para confirmar.", details: { phrase: "DESEMPAREJAR" } } },
        { status: 428 },
      );
    }
    unpairTelegram();
    return new HttpResponse(null, { status: 204 });
  }),

  http.post(`${API_BASE}/telegram/pairing/test-message`, () => HttpResponse.json({ notification_id: `notif_${Date.now()}` }, { status: 202 })),
];

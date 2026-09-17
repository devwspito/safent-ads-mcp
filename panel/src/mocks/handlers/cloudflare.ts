import { http, HttpResponse } from "msw";
import { connectCloudflareToken, disconnectCloudflareToken, getCloudflareConnection } from "../fixtures/cloudflare";
import { API_BASE } from "./apiBase";

const DISCONNECT_CONFIRMATION_PHRASE = "DESCONECTAR";
const ACCOUNT_ID_PATTERN = /^[0-9a-f]{32}$/;

export const cloudflareHandlers = [
  http.get(`${API_BASE}/integrations/cloudflare`, () => HttpResponse.json(getCloudflareConnection())),

  http.post(`${API_BASE}/integrations/cloudflare/token`, async ({ request }) => {
    const body = (await request.json()) as { token?: string; account_id?: string };
    if (!body.token?.trim()) {
      return HttpResponse.json(
        { error: { code: "VALIDATION_ERROR", message: "El token es obligatorio.", details: {} } },
        { status: 422 },
      );
    }
    if (body.account_id && !ACCOUNT_ID_PATTERN.test(body.account_id)) {
      return HttpResponse.json(
        { error: { code: "VALIDATION_ERROR", message: "El identificador de cuenta no es válido.", details: {} } },
        { status: 422 },
      );
    }
    if (body.token === "token-invalido") {
      return HttpResponse.json(
        {
          error: {
            code: "CLOUDFLARE_TOKEN_INVALID",
            message:
              "Cloudflare rechazó el token o no puede leer zonas DNS con él. Revisa los permisos (Zone.Read, DNS.Edit) y vuelve a intentarlo.",
            details: {},
          },
        },
        { status: 422 },
      );
    }
    return HttpResponse.json(connectCloudflareToken({ account_id: body.account_id }), { status: 201 });
  }),

  http.delete(`${API_BASE}/integrations/cloudflare/token`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as { typed_confirmation?: string };
    if (body.typed_confirmation?.trim().toUpperCase() !== DISCONNECT_CONFIRMATION_PHRASE) {
      return HttpResponse.json(
        {
          error: {
            code: "TYPED_CONFIRMATION_REQUIRED",
            message: `Escribe ${DISCONNECT_CONFIRMATION_PHRASE} para confirmar.`,
            details: { phrase: DISCONNECT_CONFIRMATION_PHRASE },
          },
        },
        { status: 428 },
      );
    }
    disconnectCloudflareToken();
    return new HttpResponse(null, { status: 204 });
  }),
];

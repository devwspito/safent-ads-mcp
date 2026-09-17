import { http, HttpResponse } from "msw";
import { requireMockConfirmation } from "./actionConfirmation";
import {
  type MockPlatform,
  deletePlatformAppCredentials,
  listPlatformApps,
  setGoogleAppCredentials,
  setMetaAppCredentials,
} from "../fixtures/platformApps";
import { API_BASE } from "./apiBase";

const DELETE_CONFIRMATION_PHRASE = "ELIMINAR";
const GOOGLE_CLIENT_ID_PATTERN = /^[\w-]+\.apps\.googleusercontent\.com$/;


export const platformAppsHandlers = [
  http.get(`${API_BASE}/platform-apps`, () => HttpResponse.json(listPlatformApps())),

  http.put(`${API_BASE}/platform-apps/:platform`, async ({ request, params }) => {
    const confirmation = await requireMockConfirmation(request);
    if (confirmation) return confirmation;

    const platform = params.platform as MockPlatform;
    if (platform === "google") {
      const body = (await request.json()) as {
        client_id?: string;
        client_secret?: string;
        login_customer_id?: string;
      };
      if (!body.client_id || !body.client_secret) {
        return HttpResponse.json(
          { error: { code: "VALIDATION_ERROR", message: "Credenciales inválidas.", details: {} } },
          { status: 422 },
        );
      }
      if (!GOOGLE_CLIENT_ID_PATTERN.test(body.client_id)) {
        return HttpResponse.json(
          {
            error: {
              code: "VALIDATION_ERROR",
              message: "Credenciales inválidas.",
              details: { errors: [{ field: "client_id", message: "forma inválida" }] },
            },
          },
          { status: 422 },
        );
      }
      return HttpResponse.json(
        setGoogleAppCredentials({ client_id: body.client_id, login_customer_id: body.login_customer_id }),
      );
    }

    const body = (await request.json()) as { app_id?: string; app_secret?: string };
    if (!body.app_id || !body.app_secret) {
      return HttpResponse.json(
        { error: { code: "VALIDATION_ERROR", message: "Credenciales inválidas.", details: {} } },
        { status: 422 },
      );
    }
    return HttpResponse.json(setMetaAppCredentials({ app_id: body.app_id }));
  }),

  http.delete(`${API_BASE}/platform-apps/:platform`, async ({ request, params }) => {
    const body = (await request.json()) as { typed_confirmation?: string };
    if (body.typed_confirmation?.trim().toUpperCase() !== DELETE_CONFIRMATION_PHRASE) {
      return HttpResponse.json(
        {
          error: {
            code: "TYPED_CONFIRMATION_REQUIRED",
            message: `Escribe ${DELETE_CONFIRMATION_PHRASE} para confirmar.`,
            details: { phrase: DELETE_CONFIRMATION_PHRASE },
          },
        },
        { status: 428 },
      );
    }
    deletePlatformAppCredentials(params.platform as MockPlatform);
    return new HttpResponse(null, { status: 204 });
  }),
];

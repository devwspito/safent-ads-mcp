import { http, HttpResponse } from "msw";
import type { SessionOrigin } from "@/api/schemas";
import { MOCK_BUSINESSES, MOCK_OWNER, MOCK_PASSWORD } from "../fixtures/businesses";
import { API_BASE } from "./apiBase";
import { isMockFederatedLoginAvailable } from "./federatedLogin";

/**
 * El contrato usa una cookie HttpOnly (`ads_session`) que un Service Worker no puede fijar
 * de forma fiel (los navegadores descartan `Set-Cookie` en respuestas sintéticas). Para que
 * el flujo de login sea de extremo a extremo en el modo mock, la sesión vive en memoria de
 * módulo — se reinicia al recargar la pestaña, lo cual es aceptable para un demo local.
 */
let mockSessionActive = false;

/** `session.origin`/`session.fresh_identification_until` (contracts/federated-login.md §2). */
let mockSessionOrigin: SessionOrigin = "password";
let mockFreshIdentificationUntil: string | null = null;

export const authHandlers = [
  http.post(`${API_BASE}/auth/login`, async ({ request }) => {
    const body = (await request.json()) as { email?: string; password?: string };
    if (body.password !== MOCK_PASSWORD) {
      return HttpResponse.json(
        { error: { code: "INVALID_CREDENTIALS", message: "Correo o contraseña incorrectos." } },
        { status: 401 },
      );
    }
    mockSessionActive = true;
    return new HttpResponse(null, { status: 204 });
  }),

  http.post(`${API_BASE}/auth/logout`, () => {
    mockSessionActive = false;
    return new HttpResponse(null, { status: 204 });
  }),

  http.get(`${API_BASE}/auth/me`, () => {
    if (!mockSessionActive) {
      return HttpResponse.json({ error: { code: "UNAUTHORIZED", message: "Sesión no válida." } }, { status: 401 });
    }
    return HttpResponse.json({
      ...MOCK_OWNER,
      businesses: MOCK_BUSINESSES,
      session: { origin: mockSessionOrigin, fresh_identification_until: mockFreshIdentificationUntil },
      federated_login_available: isMockFederatedLoginAvailable(),
    });
  }),
];

/** Sólo para tests: fuerza una sesión activa sin pasar por el formulario de login. */
export function setMockSessionForTests(active: boolean) {
  mockSessionActive = active;
  if (!active) {
    mockSessionOrigin = "password";
    mockFreshIdentificationUntil = null;
  }
}

/** Sólo para tests: `session.origin` que devuelve `GET /auth/me`. */
export function setMockSessionOrigin(origin: SessionOrigin) {
  mockSessionOrigin = origin;
}

/** Sólo para tests: `session.fresh_identification_until` — `null` para "no fresca". */
export function setMockFreshIdentificationUntil(value: string | null) {
  mockFreshIdentificationUntil = value;
}

/** Para que otros handlers (p.ej. `mcpOauth.ts`) devuelvan 401 sin sesión, igual que la API real. */
export function isMockSessionActive(): boolean {
  return mockSessionActive;
}

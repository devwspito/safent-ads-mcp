import { http, HttpResponse } from "msw";
import { requireMockConfirmation } from "./actionConfirmation";
import { hasMockFreshIdentification } from "./freshIdentification";
import {
  approveMockConsent,
  clearMockFederatedPresence,
  denyMockConsent,
  findMockConsent,
  listMockGrants,
  revokeMockGrant,
} from "../fixtures/mcpOauth";
import { isMockSessionActive } from "./auth";
import { API_BASE } from "./apiBase";

function unauthorized() {
  return HttpResponse.json({ error: { code: "UNAUTHORIZED", message: "Sesión no válida." } }, { status: 401 });
}

function reauthRequired() {
  return HttpResponse.json({ error: { code: "REAUTH_REQUIRED", message: "Falta X-Reauth-Token." } }, { status: 401 });
}

function txnUnavailable() {
  return HttpResponse.json({ error: { code: "TXN_EXPIRED", message: "Solicitud caducada." } }, { status: 410 });
}

export const mcpOauthHandlers = [
  http.get(`${API_BASE}/mcp-oauth/consent/:txnId`, ({ params }) => {
    if (!isMockSessionActive()) return unauthorized();
    const consent = findMockConsent(params.txnId as string);
    if (!consent) {
      return HttpResponse.json(
        { error: { code: "TXN_NOT_FOUND", message: "Solicitud no encontrada." } },
        { status: 404 },
      );
    }
    return HttpResponse.json(consent);
  }),

  http.post(`${API_BASE}/mcp-oauth/consent/:txnId/approve`, ({ request, params }) => {
    if (!isMockSessionActive()) return unauthorized();
    const txnId = params.txnId as string;
    if (!hasMockFreshIdentification(request, `approve:${txnId}`)) return reauthRequired();
    const result = approveMockConsent(txnId);
    if (!result) return txnUnavailable();
    return HttpResponse.json(result);
  }),

  http.post(`${API_BASE}/mcp-oauth/consent/:txnId/deny`, ({ params }) => {
    if (!isMockSessionActive()) return unauthorized();
    const result = denyMockConsent(params.txnId as string);
    if (!result) return txnUnavailable();
    return HttpResponse.json(result);
  }),

  http.get(`${API_BASE}/mcp-oauth/grants`, () => {
    if (!isMockSessionActive()) return unauthorized();
    return HttpResponse.json({ grants: listMockGrants() });
  }),

  http.post(`${API_BASE}/mcp-oauth/grants/:grantId/revoke`, async ({ request, params }) => {
    if (!isMockSessionActive()) return unauthorized();
    const grantId = params.grantId as string;
    if (!hasMockFreshIdentification(request, `revoke:${grantId}`)) return reauthRequired();
    const confirmation = await requireMockConfirmation(request);
    if (confirmation) return confirmation;
    revokeMockGrant(grantId);
    // La revocación consume la identificación federada (contracts/federated-login.md §2): no
    // queda "gratis" para la siguiente acción sensible, aunque siga dentro de la ventana.
    clearMockFederatedPresence();
    return new HttpResponse(null, { status: 204 });
  }),
];

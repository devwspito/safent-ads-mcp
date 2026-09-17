import { http, HttpResponse } from "msw";
import {
  approveProposal,
  batchApprove,
  getProposalDetail,
  listProposalGroups,
  patchProposalValue,
  postponeProposal,
  rejectProposal,
  setOwnerContext,
} from "../fixtures/proposals";
import { API_BASE } from "./apiBase";

const ERROR_STATUS: Record<string, number> = {
  NOT_FOUND: 404,
  DIFF_CHANGED: 409,
  PROPOSAL_EXPIRED: 409,
  TYPED_CONFIRMATION_REQUIRED: 428,
};

const ERROR_MESSAGES: Record<string, string> = {
  NOT_FOUND: "La propuesta ya no existe.",
  DIFF_CHANGED: "La propuesta cambió mientras decidías. Revisa el valor vigente.",
  PROPOSAL_EXPIRED: "La propuesta ya caducó.",
  TYPED_CONFIRMATION_REQUIRED: "Esta acción exige confirmación tecleada.",
};

function errorFor(code: string, details?: Record<string, unknown>) {
  return HttpResponse.json(
    { error: { code, message: ERROR_MESSAGES[code] ?? "No se pudo completar la acción.", details } },
    { status: ERROR_STATUS[code] ?? 400 },
  );
}

export const proposalsHandlers = [
  http.get(`${API_BASE}/campaign-drafts`, () => HttpResponse.json({ items: [], has_more: false })),
  http.get(`${API_BASE}/proposals`, ({ request }) => {
    const url = new URL(request.url);
    const lens = url.searchParams.get("lens") === "calendar_event" ? "calendar_event" : "urgency";
    return HttpResponse.json(listProposalGroups(lens));
  }),

  http.get(`${API_BASE}/proposals/:id`, ({ params }) => {
    const detail = getProposalDetail(String(params.id));
    if (!detail) return errorFor("NOT_FOUND");
    return HttpResponse.json(detail);
  }),

  http.put(`${API_BASE}/proposals/:id/owner-context`, async ({ params, request }) => {
    const body = (await request.json()) as { text: string };
    const ok = setOwnerContext(String(params.id), body.text);
    if (!ok) return errorFor("NOT_FOUND");
    return HttpResponse.json({});
  }),

  http.post(`${API_BASE}/proposals/:id/approve`, async ({ params, request }) => {
    const body = (await request.json()) as { diff_hash: string; comment?: string; typed_confirmation?: string };
    const result = approveProposal(String(params.id), body.diff_hash, body.typed_confirmation);
    if (!result.ok) {
      if (result.code === "TYPED_CONFIRMATION_REQUIRED") return errorFor(result.code, { phrase: result.phrase });
      return errorFor(result.code);
    }
    return HttpResponse.json({
      authorization_id: result.authorization_id,
      execution_id: result.execution_id,
      execution_scheduled_at: result.execution_scheduled_at,
      undo_deadline: result.undo_deadline,
      grace_seconds: result.grace_seconds,
    });
  }),

  http.post(`${API_BASE}/proposals/:id/reject`, async ({ params, request }) => {
    const body = (await request.json()) as { diff_hash: string };
    const result = rejectProposal(String(params.id), body.diff_hash);
    if (!result.ok) return errorFor("DIFF_CHANGED");
    return HttpResponse.json({});
  }),

  http.post(`${API_BASE}/proposals/:id/postpone`, async ({ params, request }) => {
    const body = (await request.json()) as { until: string };
    const result = postponeProposal(String(params.id), body.until);
    if (!result.ok) return errorFor("NOT_FOUND");
    return HttpResponse.json({});
  }),

  http.patch(`${API_BASE}/proposals/:id`, async ({ params, request }) => {
    const body = (await request.json()) as { valor_propuesto: number };
    const result = patchProposalValue(String(params.id), body.valor_propuesto);
    if (!result) return errorFor("NOT_FOUND");
    return HttpResponse.json(result);
  }),

  http.post(`${API_BASE}/proposals/batch/approve`, async ({ request }) => {
    const body = (await request.json()) as { cause_key: string; items: Array<{ proposal_id: string; diff_hash: string }> };
    const result = batchApprove(body.cause_key, body.items);
    return HttpResponse.json(result, { status: 207 });
  }),
];

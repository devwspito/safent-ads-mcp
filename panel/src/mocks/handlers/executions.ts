import { http, HttpResponse } from "msw";
import { undoCockpitEntityAction } from "../fixtures/cockpit";
import { getExecution, undoExecution, undoExecutionsBatch } from "../fixtures/proposals";
import { API_BASE } from "./apiBase";

export const executionsHandlers = [
  http.get(`${API_BASE}/executions`, () => HttpResponse.json({ items: [] })),
  http.get(`${API_BASE}/executions/:id`, ({ params }) => {
    const execution = getExecution(String(params.id));
    return execution ? HttpResponse.json(execution) : HttpResponse.json(
      { error: { code: "NOT_FOUND", message: "Ejecución no encontrada." } }, { status: 404 },
    );
  }),
  http.post(`${API_BASE}/executions/undo`, async ({ request }) => {
    const body = (await request.json()) as { execution_ids: string[]; reason: string };
    const results = undoExecutionsBatch(body.execution_ids);
    return HttpResponse.json({ results }, { status: 207 });
  }),

  http.post(`${API_BASE}/executions/:id/undo`, ({ params }) => {
    const id = String(params.id);
    const result = undoExecution(id);
    if (result.ok) {
      return HttpResponse.json({ undo_kind: result.undo_kind, execution_id: result.execution_id, compensating_proposal_id: result.compensating_proposal_id });
    }
    // No es una ejecución nacida de una propuesta: puede ser un pausar/reanudar directo
    // desde Campañas (design.md §7.2), que usa el mismo `POST /executions/{id}/undo`.
    if (undoCockpitEntityAction(id)) {
      return HttpResponse.json({ undo_kind: "cancelled", execution_id: id, compensating_proposal_id: null });
    }
    return HttpResponse.json(
      { error: { code: "UNDO_WINDOW_CLOSED", message: "La ventana de gracia ya se cerró." } },
      { status: 409 },
    );
  }),
];

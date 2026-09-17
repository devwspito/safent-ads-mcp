import { http, HttpResponse } from "msw";
import { requireMockConfirmation } from "./actionConfirmation";
import type { AutonomyGateQuestionKey, GuardrailUpdate } from "@/api/schemas/rules";
import { autonomyGate, confirmAutonomyGateQuestion, listGuardrails, listRules, updateGuardrail, updateRule } from "../fixtures/rules";
import { API_BASE } from "./apiBase";


const RULE_ERROR_STATUS: Record<string, number> = {
  AUTO_INCREASES_SPEND: 422,
  AUTONOMY_GATE_OPEN: 409,
};

const RULE_ERROR_MESSAGE: Record<string, string> = {
  AUTO_INCREASES_SPEND: "Una regla automática no puede aumentar el gasto (FR-11/FR-12).",
  AUTONOMY_GATE_OPEN: "La cuenta no tiene la puerta de autonomía confirmada.",
};

export const rulesHandlers = [
  http.get(`${API_BASE}/rules`, () => HttpResponse.json(listRules())),

  http.put(`${API_BASE}/rules/:id`, async ({ params, request }) => {
    const body = (await request.json()) as { is_enabled?: boolean; autonomy_level?: "NOTIFY" | "AUTO" | "APPROVAL"; magnitude_pct?: number };
    const result = updateRule(String(params.id), body);
    if (!result) {
      return HttpResponse.json({ error: { code: "NOT_FOUND", message: "Regla no encontrada." } }, { status: 404 });
    }
    if (!result.ok) {
      const details = result.error === "AUTONOMY_GATE_OPEN" ? { missing: result.missing } : undefined;
      return HttpResponse.json(
        { error: { code: result.error, message: RULE_ERROR_MESSAGE[result.error], details } },
        { status: RULE_ERROR_STATUS[result.error] ?? 422 },
      );
    }
    return HttpResponse.json(result.rule);
  }),

  http.get(`${API_BASE}/guardrails`, () => HttpResponse.json(listGuardrails())),

  http.put(`${API_BASE}/guardrails/:id`, async ({ params, request }) => {
    const body = (await request.json()) as GuardrailUpdate;
    const result = updateGuardrail(String(params.id), body);
    if (!result) {
      return HttpResponse.json({ error: { code: "NOT_FOUND", message: "Guardarraíl no encontrado." } }, { status: 404 });
    }
    return HttpResponse.json(result);
  }),

  http.get(`${API_BASE}/rules/autonomy-gate`, () => HttpResponse.json(autonomyGate())),

  http.post(`${API_BASE}/rules/autonomy-gate/confirmations`, async ({ request }) => {
    const confirmation = await requireMockConfirmation(request);
    if (confirmation) return confirmation;

    const body = (await request.json()) as { platform_account_id: string; key: AutonomyGateQuestionKey; value: string; comment?: string };
    const ok = confirmAutonomyGateQuestion(body);
    if (!ok) return HttpResponse.json({ error: { code: "NOT_FOUND", message: "Cuenta no encontrada." } }, { status: 404 });
    return HttpResponse.json({});
  }),
];

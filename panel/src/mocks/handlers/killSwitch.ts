import { http, HttpResponse } from "msw";
import { DEFAULT_MOCK_BUSINESS } from "../fixtures/businesses";
import { disengageBrake, engageBrake, getKillSwitchState, type EngageInput } from "../fixtures/killSwitch";
import { API_BASE } from "./apiBase";

interface SetKillSwitchBody {
  scope_kind: EngageInput["scope_kind"];
  scope_id: string | null;
  mode: EngageInput["mode"];
  engaged: boolean;
  reason: string;
  typed_confirmation?: string;
}

export const killSwitchHandlers = [
  http.get(`${API_BASE}/kill-switch`, ({ request }) => {
    const url = new URL(request.url);
    const businessId = url.searchParams.get("business_id") ?? DEFAULT_MOCK_BUSINESS.business_id;
    return HttpResponse.json(getKillSwitchState(businessId));
  }),

  http.post(`${API_BASE}/kill-switch`, async ({ request }) => {
    const body = (await request.json()) as SetKillSwitchBody;
    const businessId = body.scope_kind === "business" ? (body.scope_id ?? DEFAULT_MOCK_BUSINESS.business_id) : DEFAULT_MOCK_BUSINESS.business_id;

    if (body.engaged) {
      engageBrake({ scope_kind: body.scope_kind, scope_id: body.scope_id, mode: body.mode, reason: body.reason });
    } else {
      const result = disengageBrake({
        scope_kind: body.scope_kind,
        scope_id: body.scope_id,
        mode: body.mode,
        typed_confirmation: body.typed_confirmation,
      });
      if (!result.ok) {
        return HttpResponse.json(
          { error: { code: result.code, message: "Escribe REACTIVAR para confirmar.", details: { phrase: result.phrase } } },
          { status: 428 },
        );
      }
    }

    return HttpResponse.json(getKillSwitchState(businessId));
  }),
];

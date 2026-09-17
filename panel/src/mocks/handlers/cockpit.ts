import { http, HttpResponse } from "msw";
import { DEFAULT_MOCK_BUSINESS } from "../fixtures/businesses";
import { buildCockpit, markCockpitExecutionUndone } from "../fixtures/cockpit";
import { API_BASE } from "./apiBase";

// Use the real proposal/undo contracts; never invent cockpit write endpoints.
export const cockpitHandlers = [
  http.get(`${API_BASE}/cockpit`, ({ request }) => {
    const url = new URL(request.url);
    const businessId = url.searchParams.get("business_id") ?? DEFAULT_MOCK_BUSINESS.business_id;
    const window = (url.searchParams.get("window") as "today" | "7d" | "30d" | null) ?? "7d";
    return HttpResponse.json(buildCockpit(businessId, window));
  }),
  http.post(`${API_BASE}/proposals/prop_buy_1/approve`, async ({ request }) => {
    const body = await request.json() as { diff_hash?: string };
    if (body.diff_hash !== "cockpit-diff-buy-1") {
      return HttpResponse.json({ error: { code: "DIFF_CHANGED", message: "El cambio ha cambiado." } }, { status: 409 });
    }
    return HttpResponse.json({ ok: true });
  }),
  http.post(`${API_BASE}/executions/exec_sell_1/undo`, () => {
    markCockpitExecutionUndone("exec_sell_1");
    return HttpResponse.json({ ok: true });
  }),
];

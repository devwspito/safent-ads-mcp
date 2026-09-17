import { http, HttpResponse } from "msw";
import { DEFAULT_MOCK_BUSINESS } from "../fixtures/businesses";
import { buildBadges } from "../fixtures/badges";
import { API_BASE } from "./apiBase";

export const badgesHandlers = [
  http.get(`${API_BASE}/badges`, ({ request }) => {
    const url = new URL(request.url);
    const businessId = url.searchParams.get("business_id") ?? DEFAULT_MOCK_BUSINESS.business_id;
    const signalsSince = url.searchParams.get("signals_since") ?? undefined;
    return HttpResponse.json(buildBadges(businessId, signalsSince));
  }),
];

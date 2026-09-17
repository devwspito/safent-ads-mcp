import { http, HttpResponse } from "msw";
import { DEFAULT_MOCK_BUSINESS } from "../fixtures/businesses";
import { buildEntityChildren } from "../fixtures/entities";
import { buildPortfolio } from "../fixtures/portfolio";
import { buildSignalsResponse } from "../fixtures/signals";
import { API_BASE } from "./apiBase";

export const portfolioSignalsHandlers = [
  http.get(`${API_BASE}/portfolio`, ({ request }) => {
    const url = new URL(request.url);
    const businessId = url.searchParams.get("business_id") ?? DEFAULT_MOCK_BUSINESS.business_id;
    return HttpResponse.json(buildPortfolio(businessId));
  }),

  http.get(`${API_BASE}/freshness`, () => {
    return HttpResponse.json({ last_ingested_at: new Date(Date.now() - 12 * 60_000).toISOString(), lag_minutes: 12, is_stale: false });
  }),

  http.get(`${API_BASE}/entities/:ref/children`, ({ params }) => {
    const ref = decodeURIComponent(String(params.ref));
    return HttpResponse.json(buildEntityChildren(ref));
  }),

  http.get(`${API_BASE}/signals`, ({ request }) => {
    const url = new URL(request.url);
    return HttpResponse.json(
      buildSignalsResponse({
        platform: url.searchParams.get("platform") ?? undefined,
        kind: url.searchParams.get("kind") ?? undefined,
        min_strength: url.searchParams.has("min_strength") ? Number(url.searchParams.get("min_strength")) : undefined,
      }),
    );
  }),
];

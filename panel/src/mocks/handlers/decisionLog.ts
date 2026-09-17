import { http, HttpResponse } from "msw";
import { listDecisionLog, verifyDecisionLogChain } from "../fixtures/decisionLog";
import { API_BASE } from "./apiBase";

export const decisionLogHandlers = [
  http.get(`${API_BASE}/decision-log`, ({ request }) => {
    const url = new URL(request.url);
    const eventType = url.searchParams.get("event_type");
    const entityRef = url.searchParams.get("entity_ref");
    const { items, next_cursor } = listDecisionLog();
    const filtered = items.filter(
      (item) => (!eventType || item.event_type === eventType) && (!entityRef || item.entity_ref === entityRef),
    );
    return HttpResponse.json({ items: filtered, next_cursor });
  }),

  http.get(`${API_BASE}/decision-log/verify`, () => HttpResponse.json(verifyDecisionLogChain())),
];

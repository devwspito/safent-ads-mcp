import { http, HttpResponse } from "msw";
import { API_BASE } from "./apiBase";

export const storeApiStatus = {
  available: true, configured: false, base_url: "https://catalog.example/api", egress_ip: "203.0.113.10",
  updated_at: null, resources: ["catalog", "store-catalog", "stock"], read_only: true,
};
export const storeApiHandlers = [
  http.get(`${API_BASE}/integrations/store-api`, () => HttpResponse.json(storeApiStatus)),
  http.get(`${API_BASE}/launch-plans`, () => HttpResponse.json({ items: [] })),
];

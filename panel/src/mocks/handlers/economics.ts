import { http, HttpResponse } from "msw";
import { requireMockConfirmation } from "./actionConfirmation";
import {
  CsvHeaderFixtureError,
  generateWebhookToken,
  importConversionsCsv,
  listOfferings,
  updateOfferingEconomics,
} from "../fixtures/economics";
import { DEFAULT_MOCK_BUSINESS } from "../fixtures/businesses";
import { API_BASE } from "./apiBase";
import type { OfferingEconomicsInput } from "@/api/schemas/economics";

const NOT_FOUND = { error: { code: "NOT_FOUND", message: "No encontrado." } };

function businessIdOf(request: Request): string {
  return new URL(request.url).searchParams.get("business_id") ?? DEFAULT_MOCK_BUSINESS.business_id;
}

function validationError(message: string) {
  return HttpResponse.json({ error: { code: "VALIDATION_ERROR", message } }, { status: 422 });
}

export const economicsHandlers = [
  http.get(`${API_BASE}/offerings`, ({ request }) => HttpResponse.json(listOfferings(businessIdOf(request)))),

  http.put(`${API_BASE}/offerings/:id/economics`, async ({ params, request }) => {
    const body = (await request.json()) as OfferingEconomicsInput;
    if (body.vat_rate_pct < 0 || body.vat_rate_pct > 100) return validationError("vat_rate_pct fuera de [0,100].");
    if (body.refund_rate_pct !== null && (body.refund_rate_pct < 0 || body.refund_rate_pct > 100)) {
      return validationError("refund_rate_pct fuera de [0,100].");
    }
    if (body.delivery_cost_minor < 0 || body.sales_cost_minor < 0) return validationError("los importes deben ser >= 0.");
    const updated = updateOfferingEconomics(businessIdOf(request), String(params.id), body);
    if (!updated) return HttpResponse.json(NOT_FOUND, { status: 404 });
    return HttpResponse.json(updated);
  }),

  http.post(`${API_BASE}/conversions/import`, async ({ request }) => {
    const formData = await request.formData();
    const file = formData.get("file");
    // `instanceof File` es frágil aquí: `fetch`/`FormData` de test (undici, `src/test/setup.ts`)
    // y el `File` que arrastra/elige el usuario (jsdom) no comparten clase entre sí -- se
    // comprueba por forma (`.text` como función), no por identidad de clase.
    if (!file || typeof file === "string" || typeof file.text !== "function") {
      return validationError("Falta el fichero.");
    }
    try {
      return HttpResponse.json(importConversionsCsv(await file.text()));
    } catch (error) {
      if (error instanceof CsvHeaderFixtureError) return validationError(error.message);
      throw error;
    }
  }),

  http.post(`${API_BASE}/conversions/webhook-token`, async ({ request }) => {
    const confirmation = await requireMockConfirmation(request);
    if (confirmation) return confirmation;
    return HttpResponse.json(generateWebhookToken());
  }),
];

export { resetEconomicsFixtures } from "../fixtures/economics";

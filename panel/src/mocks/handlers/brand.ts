import { http, HttpResponse } from "msw";
import type { AssetKind, ConfirmBrandDraftInput, UpdateBrandClaimsInput } from "@/api/schemas/brand";
import {
  confirmBrandDraftFixture,
  discoverBrandFixture,
  getBrandDraftFixture,
  getBrandKitFixture,
  updateBrandClaimsFixture,
  uploadBrandAssetFixture,
} from "../fixtures/brand";
import { API_BASE } from "./apiBase";

const NO_KIT_ERROR = { error: { code: "ENTITY_NOT_FOUND", message: "No hay kit de marca para este negocio." } };
const NO_DRAFT_ERROR = { error: { code: "ENTITY_NOT_FOUND", message: "No hay borrador de marca para este negocio." } };

function businessIdOf(request: Request): string {
  return new URL(request.url).searchParams.get("business_id") ?? "";
}

export const brandHandlers = [
  http.get(`${API_BASE}/brand`, ({ request }) => {
    const kit = getBrandKitFixture(businessIdOf(request));
    if (!kit) return HttpResponse.json(NO_KIT_ERROR, { status: 404 });
    return HttpResponse.json(kit);
  }),

  http.get(`${API_BASE}/brand/draft`, ({ request }) => {
    const draft = getBrandDraftFixture(businessIdOf(request));
    if (!draft) return HttpResponse.json(NO_DRAFT_ERROR, { status: 404 });
    return HttpResponse.json(draft);
  }),

  http.post(`${API_BASE}/brand/discover`, async ({ request }) => {
    const body = (await request.json()) as { url: string };
    return HttpResponse.json(discoverBrandFixture(businessIdOf(request), body.url));
  }),

  http.post(`${API_BASE}/brand/assets`, async ({ request }) => {
    const url = new URL(request.url);
    const kind = (url.searchParams.get("kind") ?? "logo_vector") as AssetKind;
    const formData = await request.formData();
    const file = formData.get("file");
    // `instanceof File` es frágil entre `fetch` de test (undici, `src/test/setup.ts`) y el
    // `File` que sube el usuario (jsdom): se comprueba por forma, no por identidad de clase.
    if (!file || typeof file === "string" || typeof file.name !== "string") {
      return HttpResponse.json({ error: { code: "VALIDATION_ERROR", message: "Falta el archivo." } }, { status: 422 });
    }
    return HttpResponse.json(uploadBrandAssetFixture(businessIdOf(request), kind, file.name), { status: 201 });
  }),

  http.post(`${API_BASE}/brand/confirm`, async ({ request }) => {
    const body = (await request.json()) as ConfirmBrandDraftInput;
    const result = confirmBrandDraftFixture(businessIdOf(request), body);
    if (!result.ok) {
      if (result.error === "NO_DRAFT") return HttpResponse.json(NO_DRAFT_ERROR, { status: 404 });
      return HttpResponse.json(
        { error: { code: "VALIDATION_ERROR", message: "Uno de los activos seleccionados ya no esta en el borrador actual." } },
        { status: 422 },
      );
    }
    return HttpResponse.json(result.kit);
  }),

  http.put(`${API_BASE}/brand/claims`, async ({ request }) => {
    const body = (await request.json()) as UpdateBrandClaimsInput;
    const result = updateBrandClaimsFixture(businessIdOf(request), body);
    if (!result.ok) return HttpResponse.json(NO_KIT_ERROR, { status: 404 });
    return HttpResponse.json(result.kit);
  }),
];

export { resetBrandFixtures, setBrandKitFixture } from "../fixtures/brand";

import { http, HttpResponse } from "msw";
import {
  approvePackage,
  getCreativeCandidates,
  getPackageDetail,
  patchAdCreative,
  rejectPackage,
  resumePackage,
  undoPackage,
} from "../fixtures/packages";
import { API_BASE } from "./apiBase";

const PACKAGE_ERROR_STATUS: Record<string, number> = {
  NOT_FOUND: 404,
  PACKAGE_CHANGED: 409,
  PACKAGE_NOT_PROPOSED: 409,
  PACKAGE_NOT_EDITABLE: 409,
  PACKAGE_NOT_RESUMABLE: 409,
  UNDO_WINDOW_CLOSED: 409,
  BRAKE_ENGAGED: 409,
};

const PACKAGE_ERROR_MESSAGES: Record<string, string> = {
  NOT_FOUND: "El paquete ya no existe.",
  PACKAGE_CHANGED: "El paquete cambió mientras decidías. Revisa el detalle vigente.",
  PACKAGE_NOT_PROPOSED: "El paquete ya no está pendiente de decisión.",
  PACKAGE_NOT_EDITABLE: "El paquete ya no se puede editar.",
  PACKAGE_NOT_RESUMABLE: "El paquete no se puede continuar ahora mismo.",
  UNDO_WINDOW_CLOSED: "Ya no se puede deshacer.",
  BRAKE_ENGAGED: "Los cambios están parados.",
};

function packageErrorFor(code: string) {
  return HttpResponse.json(
    { error: { code, message: PACKAGE_ERROR_MESSAGES[code] ?? "No se pudo completar la acción." } },
    { status: PACKAGE_ERROR_STATUS[code] ?? 400 },
  );
}

export const packagesHandlers = [
  http.get(`${API_BASE}/packages/:id`, ({ params }) => {
    const detail = getPackageDetail(String(params.id));
    if (!detail) return HttpResponse.json({ error: { code: "NOT_FOUND", message: "El paquete ya no existe." } }, { status: 404 });
    return HttpResponse.json(detail);
  }),

  http.post(`${API_BASE}/packages/:id/approve`, async ({ params, request }) => {
    const url = new URL(request.url);
    const businessId = url.searchParams.get("business_id") ?? "biz_ejemplo";
    const body = (await request.json()) as { package_hash: string; comment?: string };
    const result = approvePackage(String(params.id), body.package_hash, businessId);
    if (!result.ok) return packageErrorFor(result.code);
    return HttpResponse.json({
      publication_id: result.publication_id,
      authorization_id: result.authorization_id,
      grace_seconds: result.grace_seconds,
      execution_starts_at: result.execution_starts_at,
      approval_expires_at: result.approval_expires_at,
      undo: result.undo,
    });
  }),

  http.post(`${API_BASE}/packages/:id/reject`, async ({ params, request }) => {
    const body = (await request.json()) as { package_hash: string; comment?: string };
    const result = rejectPackage(String(params.id), body.package_hash);
    if (!result.ok) return packageErrorFor(result.code);
    return HttpResponse.json({});
  }),

  http.post(`${API_BASE}/packages/:id/resume`, async ({ params, request }) => {
    const body = (await request.json()) as { package_hash: string };
    const result = resumePackage(String(params.id), body.package_hash);
    if (!result.ok) return packageErrorFor(result.code);
    return HttpResponse.json({ publication_id: result.publication_id, approval_expires_at: result.approval_expires_at }, { status: 202 });
  }),

  http.post(`${API_BASE}/packages/:id/undo`, async ({ params, request }) => {
    const body = (await request.json()) as { package_hash: string; reason: string };
    const result = undoPackage(String(params.id), body.package_hash);
    if (!result.ok) return packageErrorFor(result.code);
    return HttpResponse.json(
      result.undo_kind === "cancelled_publication"
        ? { undo_kind: result.undo_kind, sentence: result.sentence }
        : { undo_kind: result.undo_kind, campaign_entity_ref: result.campaign_entity_ref, execution_id: result.execution_id, sentence: result.sentence },
    );
  }),

  http.get(`${API_BASE}/packages/:id/creative-candidates`, ({ params, request }) => {
    const url = new URL(request.url);
    const adLocalRef = url.searchParams.get("ad_local_ref");
    if (!adLocalRef) return HttpResponse.json({ error: { code: "VALIDATION_ERROR", message: "Falta ad_local_ref." } }, { status: 400 });
    const result = getCreativeCandidates(String(params.id), adLocalRef);
    if (!result) return HttpResponse.json({ error: { code: "NOT_FOUND", message: "El paquete ya no existe." } }, { status: 404 });
    return HttpResponse.json(result);
  }),

  http.patch(`${API_BASE}/packages/:id/ads/:adLocalRef/creative`, async ({ params, request }) => {
    const body = (await request.json()) as { package_hash: string; creative_asset_id: string };
    const result = patchAdCreative(String(params.id), String(params.adLocalRef), body.package_hash, body.creative_asset_id);
    if (!result.ok) return packageErrorFor(result.code);
    return HttpResponse.json({ package_hash: result.package_hash });
  }),
];

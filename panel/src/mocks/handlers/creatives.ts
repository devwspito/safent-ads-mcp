import { http, HttpResponse } from "msw";
import { approveCreative, listCreatives, regenerateCreative, rejectCreative } from "../fixtures/creatives";
import { getRegenerateJob, startRegenerateJob } from "../fixtures/packages";
import { API_BASE } from "./apiBase";

export const creativesHandlers = [
  http.get(`${API_BASE}/creatives`, ({ request }) => {
    const url = new URL(request.url);
    const pendingApprovalParam = url.searchParams.get("pending_approval");
    return HttpResponse.json(
      listCreatives({
        signal: url.searchParams.get("signal") ?? undefined,
        pendingApproval: pendingApprovalParam === null ? undefined : pendingApprovalParam === "true",
      }),
    );
  }),

  http.post(`${API_BASE}/creatives/:id/policy-check`, () => {
    return HttpResponse.json({ verdict: "PASS", findings: [] });
  }),

  http.post(`${API_BASE}/creatives/:id/propose-publication`, ({ params }) => {
    const assetId = String(params.id);
    approveCreative(assetId);
    return HttpResponse.json(
      { proposal_id: `prop_publish_${assetId}`, diff_hash: `hash_${assetId}`, expires_at: new Date(Date.now() + 4 * 3_600_000).toISOString() },
      { status: 201 },
    );
  }),

  http.post(`${API_BASE}/creatives/:id/reject`, ({ params }) => {
    const assetId = String(params.id);
    const ok = rejectCreative(assetId);
    if (!ok) return HttpResponse.json({ error: { code: "NOT_FOUND", message: "Creatividad no encontrada." } }, { status: 404 });
    return HttpResponse.json({ asset_id: assetId, review_state: "rejected" });
  }),

  /**
   * `003-paquete-de-campana/contracts/api.md` §6: «Regenerar» reutiliza este mismo extremo para
   * los activos de un paquete. El catálogo de Creatividades (SEEDS) se prueba primero para no
   * tocar su comportamiento existente; los IDs propios de un paquete (`pkgcr_*`) caen al
   * registro de `fixtures/packages.ts`.
   */
  http.post(`${API_BASE}/creatives/:id/regenerate`, ({ params }) => {
    const assetId = String(params.id);
    if (regenerateCreative(assetId)) return HttpResponse.json({ job_id: `job_regen_${assetId}` }, { status: 202 });
    const packageJob = startRegenerateJob(assetId);
    if (packageJob) return HttpResponse.json(packageJob, { status: 202 });
    return HttpResponse.json({ error: { code: "NOT_FOUND", message: "Creatividad no encontrada." } }, { status: 404 });
  }),

  http.get(`${API_BASE}/creative-jobs/:id`, ({ params }) => {
    const job = getRegenerateJob(String(params.id));
    if (!job) return HttpResponse.json({ error: { code: "NOT_FOUND", message: "El trabajo ya no existe." } }, { status: 404 });
    return HttpResponse.json(job);
  }),
];

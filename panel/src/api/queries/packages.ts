/** `contracts/api.md` §2, §4, §5, §6. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { acknowledgedSchema } from "@/api/schemas/proposals";
import {
  approvePackageResponseSchema,
  packageCreativeCandidatesResponseSchema,
  packagePreviewSchema,
  patchPackageAdCreativeResponseSchema,
  resumePackageResponseSchema,
  undoPackageResponseSchema,
  type PackagePreview,
} from "@/api/schemas/packages";

function packageDetailKey(businessId: string, packageId: string | null) {
  return ["package-detail", businessId, packageId] as const;
}

/**
 * Mientras la publicación corre, el detalle se consulta cada 10 s — design.md §2.6
 * ("Confirmación de la plataforma: 1–60 s, consulta cada 10 s, sin bloquear nada más").
 * Fuera de esos tres estados el detalle es estable y no hace falta seguir sondeando.
 */
const POLLING_STATES = new Set(["approved", "publishing", "verifying"]);
const PACKAGE_POLL_INTERVAL_MS = 10_000;

export function usePackageDetail(businessId: string, packageId: string | null) {
  return useQuery<PackagePreview>({
    queryKey: packageDetailKey(businessId, packageId),
    queryFn: () => apiClient.get(`/packages/${packageId}`, packagePreviewSchema, { business_id: businessId }),
    enabled: Boolean(businessId && packageId),
    refetchInterval: (query) => (query.state.data && POLLING_STATES.has(query.state.data.state) ? PACKAGE_POLL_INTERVAL_MS : false),
  });
}

function invalidatePackageAndFeed(queryClient: ReturnType<typeof useQueryClient>, businessId: string, packageId: string) {
  void queryClient.invalidateQueries({ queryKey: packageDetailKey(businessId, packageId) });
  void queryClient.invalidateQueries({ queryKey: ["proposals", businessId] });
}

/** `POST /packages/{id}/approve` — §4/R2.C. `package_hash` es obligatorio (INV-8 en el borde HTTP). */
export function useApprovePackage(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { packageId: string; packageHash: string; comment?: string }) =>
      apiClient.post(
        `/packages/${input.packageId}/approve`,
        approvePackageResponseSchema,
        { package_hash: input.packageHash, comment: input.comment },
        { business_id: businessId },
      ),
    onSuccess: (_result, variables) => invalidatePackageAndFeed(queryClient, businessId, variables.packageId),
  });
}

/** `POST /packages/{id}/reject` — §4. */
export function useRejectPackage(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { packageId: string; packageHash: string; comment?: string }) =>
      apiClient.post(
        `/packages/${input.packageId}/reject`,
        acknowledgedSchema,
        { package_hash: input.packageHash, comment: input.comment },
        { business_id: businessId },
      ),
    onSuccess: (_result, variables) => invalidatePackageAndFeed(queryClient, businessId, variables.packageId),
  });
}

/**
 * `POST /packages/{id}/resume` (Revisión 2, R2.C) — reanudar es decidir otra vez: exige la
 * huella vigente. `409 PACKAGE_APPROVAL_EXPIRED` pide volver a aprobar sobre la misma huella.
 */
export function useResumePackage(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { packageId: string; packageHash: string }) =>
      apiClient.post(
        `/packages/${input.packageId}/resume`,
        resumePackageResponseSchema,
        { package_hash: input.packageHash },
        { business_id: businessId },
      ),
    onSuccess: (_result, variables) => invalidatePackageAndFeed(queryClient, businessId, variables.packageId),
  });
}

/**
 * `POST /packages/{id}/undo` (§5, R2.C) — antes de la primera escritura cancela sin crear
 * nada; tras la activación y dentro de la ventana, pausa la campaña. Nunca borra.
 */
export function useUndoPackage(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { packageId: string; packageHash: string; reason: string }) =>
      apiClient.post(
        `/packages/${input.packageId}/undo`,
        undoPackageResponseSchema,
        { package_hash: input.packageHash, reason: input.reason },
        { business_id: businessId },
      ),
    onSuccess: (_result, variables) => invalidatePackageAndFeed(queryClient, businessId, variables.packageId),
  });
}

/** Sólo se piden al abrir «Cambiar imagen» de un anuncio concreto — `ad_local_ref` es obligatorio. */
export function usePackageCreativeCandidates(businessId: string, packageId: string, adLocalRef: string | null) {
  return useQuery({
    queryKey: ["package-creative-candidates", businessId, packageId, adLocalRef],
    queryFn: () =>
      apiClient.get(`/packages/${packageId}/creative-candidates`, packageCreativeCandidatesResponseSchema, {
        business_id: businessId,
        ad_local_ref: adLocalRef ?? undefined,
      }),
    enabled: Boolean(businessId && packageId && adLocalRef),
  });
}

/**
 * Re-apunta la referencia de un anuncio a un activo ya listo — huella nueva, invalida la
 * aprobación previa. `ad_local_ref` puede llevar "/" (`"as#1/ad#2"`, contracts/api.md §Tipos
 * comunes) así que hay que codificarlo como UN solo segmento de ruta — si no, `as#1/ad#2`
 * se colaría como dos segmentos y la petición no encontraría la ruta.
 */
export function usePatchPackageAdCreative(businessId: string, packageId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { adLocalRef: string; packageHash: string; creativeAssetId: string }) =>
      apiClient.patch(
        `/packages/${packageId}/ads/${encodeURIComponent(input.adLocalRef)}/creative`,
        patchPackageAdCreativeResponseSchema,
        { package_hash: input.packageHash, creative_asset_id: input.creativeAssetId },
        { business_id: businessId },
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: packageDetailKey(businessId, packageId) }),
  });
}

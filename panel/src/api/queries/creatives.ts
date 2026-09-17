import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import {
  creativeJobSchema,
  creativeRegenerateResponseSchema,
  creativeRejectResponseSchema,
  creativesResponseSchema,
  policyCheckResponseSchema,
  proposePublicationResponseSchema,
  type CreativeJob,
  type ProposePublicationInput,
} from "@/api/schemas/creatives";

export interface CreativesFilters {
  business_id: string;
  signal?: string;
  media_kind?: string;
  policy_verdict?: string;
  pending_approval?: boolean;
}

export function useCreatives(filters: CreativesFilters) {
  return useQuery({
    queryKey: ["creatives", filters],
    queryFn: () =>
      apiClient.get("/creatives", creativesResponseSchema, {
        business_id: filters.business_id,
        signal: filters.signal,
        media_kind: filters.media_kind,
        policy_verdict: filters.policy_verdict,
        pending_approval: filters.pending_approval,
      }),
    enabled: Boolean(filters.business_id),
  });
}

export function useCreativePolicyCheck() {
  return useMutation({
    mutationFn: (input: { assetId: string; platform: string; placement: string }) =>
      apiClient.post(`/creatives/${input.assetId}/policy-check`, policyCheckResponseSchema, {
        platform: input.platform,
        placement: input.placement,
      }),
  });
}

/** Publicar nunca escribe directo: crea una propuesta (`propose_creative_publication`, FR-33). */
export function usePublishCreative(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { assetId: string } & ProposePublicationInput) =>
      apiClient.post(`/creatives/${input.assetId}/propose-publication`, proposePublicationResponseSchema, {
        ad_set_ref: input.ad_set_ref,
        ad_copy: input.ad_copy,
        extra_asset_ids: input.extra_asset_ids,
        typed_confirmation: input.typed_confirmation,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["creatives", { business_id: businessId }] });
      void queryClient.invalidateQueries({ queryKey: ["proposals", businessId] });
    },
  });
}

export function useRejectCreative(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { assetId: string; reason: string }) =>
      apiClient.post(`/creatives/${input.assetId}/reject`, creativeRejectResponseSchema, { reason: input.reason }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["creatives", { business_id: businessId }] }),
  });
}

export function useRegenerateCreative(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { assetId: string; reason: string }) =>
      apiClient.post(`/creatives/${input.assetId}/regenerate`, creativeRegenerateResponseSchema, { reason: input.reason }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["creatives", { business_id: businessId }] }),
  });
}

/**
 * Sondeo de `POST /creatives/{id}/regenerate` — `rest-api.md` §Creatividades y
 * `003-paquete-de-campana/contracts/api.md` §6 ("«Regenerar» … consulta `GET
 * /creative-jobs/{job_id}`"). Deja de repreguntar en cuanto el trabajo llega a un estado
 * terminal (`READY`/`FAILED`/`FALLBACK_CLOUD`).
 */
const CREATIVE_JOB_TERMINAL_STATES = new Set(["READY", "FAILED", "FALLBACK_CLOUD"]);

export function useCreativeJob(jobId: string | null) {
  return useQuery<CreativeJob>({
    queryKey: ["creative-job", jobId],
    queryFn: () => apiClient.get(`/creative-jobs/${jobId}`, creativeJobSchema),
    enabled: Boolean(jobId),
    refetchInterval: (query) => (query.state.data && CREATIVE_JOB_TERMINAL_STATES.has(query.state.data.state) ? false : 2_000),
  });
}

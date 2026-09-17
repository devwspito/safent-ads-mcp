import { useInfiniteQuery, useMutation, useQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { useMe } from "./auth";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { apiClient } from "@/api/client";
import type { CampaignCreationPlan } from "@/api/schemas/campaignCreation";
import {
  acknowledgedSchema,
  approveResponseSchema,
  batchApproveResponseSchema,
  patchProposalResponseSchema,
  proposalDetailSchema,
  proposalsResponseSchema,
  type ProposalsResponse,
  type ProposalDetail,
} from "@/api/schemas/proposals";

export interface ProposalsFilters {
  business_id: string;
  lens: "urgency" | "calendar_event";
  state?: string;
  cause_key?: string;
}

function invalidateProposals(queryClient: ReturnType<typeof useQueryClient>, businessId: string) {
  void queryClient.invalidateQueries({ queryKey: ["proposals", businessId] });
  void queryClient.invalidateQueries({ queryKey: ["proposal-detail"] });
  void queryClient.invalidateQueries({ queryKey: ["cockpit", businessId] });
}

/**
 * Carga al desplazar, sin cursor visible — panel-interaction-spec.md §1.2 ("se va la
 * paginación con cursor visible"). `fetchNextPage` la dispara el observador de scroll
 * de `PropuestasPage`, nunca un botón "Siguiente".
 */
export function useProposals(filters: ProposalsFilters) {
  return useInfiniteQuery<ProposalsResponse, Error, InfiniteData<ProposalsResponse>, readonly unknown[], string | undefined>({
    queryKey: ["proposals", filters.business_id, filters.lens, filters.state, filters.cause_key],
    queryFn: ({ pageParam }: { pageParam: string | undefined }) =>
      apiClient.get("/proposals", proposalsResponseSchema, {
        business_id: filters.business_id,
        lens: filters.lens,
        state: filters.state,
        cause_key: filters.cause_key,
        cursor: pageParam,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: Boolean(filters.business_id),
    refetchInterval: 45_000,
    // Propuestas es la superficie donde se decide — a diferencia del resto de consultas
    // (item 10), sí interesa refrescarla al volver a la pestaña.
    refetchOnWindowFocus: true,
    placeholderData: (previous) => previous,
  });
}

export function useProposalDetail(proposalId: string | null) {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  return useQuery<ProposalDetail>({
    queryKey: ["proposal-detail", businessId, proposalId],
    queryFn: () => apiClient.get(`/proposals/${proposalId}`, proposalDetailSchema, { business_id: businessId }),
    enabled: Boolean(proposalId && businessId),
  });
}

export function useApproveProposal(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { proposalId: string; diffHash: string; comment?: string; typedConfirmation?: string }) =>
      apiClient.post(`/proposals/${input.proposalId}/approve`, approveResponseSchema, {
        diff_hash: input.diffHash,
        comment: input.comment,
        typed_confirmation: input.typedConfirmation,
      }),
    onSuccess: () => invalidateProposals(queryClient, businessId),
  });
}

/** `text` ≤500 caracteres (rest-api.md §Propuestas). */
export function useSetProposalOwnerContext(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { proposalId: string; text: string }) =>
      apiClient.put(`/proposals/${input.proposalId}/owner-context`, acknowledgedSchema, { text: input.text }),
    onSuccess: () => invalidateProposals(queryClient, businessId),
  });
}

export function useRejectProposal(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { proposalId: string; diffHash: string; comment?: string }) =>
      apiClient.post(`/proposals/${input.proposalId}/reject`, acknowledgedSchema, {
        diff_hash: input.diffHash,
        comment: input.comment,
      }),
    onSuccess: () => invalidateProposals(queryClient, businessId),
  });
}

export function usePostponeProposal(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { proposalId: string; until: string }) =>
      apiClient.post(`/proposals/${input.proposalId}/postpone`, acknowledgedSchema, {
        until: input.until,
      }),
    onSuccess: () => invalidateProposals(queryClient, businessId),
  });
}

export function useEditProposalValue(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { proposalId: string; valorPropuesto: number }) =>
      apiClient.patch(`/proposals/${input.proposalId}`, patchProposalResponseSchema, {
        valor_propuesto: input.valorPropuesto,
      }),
    onSuccess: () => invalidateProposals(queryClient, businessId),
  });
}

export function useEditCampaignCreation(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { proposalId: string; diffHash: string; creationPlan: CampaignCreationPlan }) =>
      apiClient.patch(`/proposals/${input.proposalId}`, patchProposalResponseSchema, {
        diff_hash: input.diffHash, creation_plan: input.creationPlan,
      }),
    onSuccess: () => invalidateProposals(queryClient, businessId),
    retry: false,
  });
}

export function useBatchApprove(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { causeKey: string; items: Array<{ proposalId: string; diffHash: string }> }) =>
      apiClient.post("/proposals/batch/approve", batchApproveResponseSchema, {
        cause_key: input.causeKey,
        items: input.items.map((item) => ({ proposal_id: item.proposalId, diff_hash: item.diffHash })),
      }, { business_id: businessId }),
    onSuccess: () => invalidateProposals(queryClient, businessId),
  });
}

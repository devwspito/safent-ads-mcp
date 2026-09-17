import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { apiClient } from "@/api/client";

const brief = z.object({
  title: z.string().nullable(), platform: z.enum(["google", "meta"]).nullable(),
  daily_budget: z.object({ amount: z.string(), currency: z.literal("EUR") }).nullable(),
  landing_url: z.string().nullable(), notes: z.string().nullable(),
}).passthrough();
export const campaignDraftSchema = z.object({
  draft_id: z.string().uuid(), draft_key: z.string(), business_id: z.string().uuid(),
  revision: z.number().int().positive(), state: z.enum(["draft", "proposed"]), brief,
  missing_fields: z.array(z.string()), proposal_id: z.string().uuid().nullable(),
  executable: z.literal(false), updated_at: z.string(),
}).strict();
export type CampaignDraft = z.infer<typeof campaignDraftSchema>;
const listSchema = z.object({ items: z.array(campaignDraftSchema), has_more: z.boolean() }).strict();

export function useCampaignDrafts(businessId: string) {
  return useQuery({ queryKey: ["campaign-drafts", businessId], enabled: Boolean(businessId),
    queryFn: () => apiClient.get("/campaign-drafts", listSchema, { business_id: businessId }),
  });
}
export function useSaveCampaignDraft(businessId: string) {
  const client = useQueryClient();
  return useMutation({ mutationFn: (body: { draft_key: string; expected_revision?: number; changes: Record<string, unknown> }) => apiClient.post("/campaign-drafts", campaignDraftSchema, body, { business_id: businessId }), retry: false,
    onSuccess: () => { void client.invalidateQueries({ queryKey: ["campaign-drafts", businessId] }); },
  });
}
export function usePromoteCampaignDraft(businessId: string) {
  const client = useQueryClient();
  return useMutation({ mutationFn: (draft: CampaignDraft) => apiClient.post(`/campaign-drafts/${draft.draft_id}/propose`, campaignDraftSchema, { expected_revision: draft.revision }, { business_id: businessId }), retry: false,
    onSuccess: () => { void client.invalidateQueries({ queryKey: ["campaign-drafts", businessId] }); void client.invalidateQueries({ queryKey: ["proposals", businessId] }); },
  });
}

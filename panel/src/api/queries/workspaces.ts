import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { apiClient } from "@/api/client";
import { campaignDraftSchema } from "./campaignDrafts";

export const resourceSchema = z.object({ key: z.string(), kind: z.enum(["video", "script", "landing", "tracking", "whatsapp", "document"]), title: z.string(), status: z.enum(["pending", "in_progress", "ready"]), url: z.string().nullable(), notes: z.string().nullable() });
const workspaceSchema = z.object({
  id: z.string().uuid(), business_id: z.string().uuid(), workspace_key: z.string(), revision: z.number(),
  contract_version: z.literal(1), updated_at: z.string(),
  brief: z.object({ title: z.string(), objective: z.string().nullable(), schedule: z.string().nullable(), total_budget: z.object({ amount: z.string(), currency: z.literal("EUR") }).nullable(), notes: z.string().nullable(), source_slug: z.string().nullable(), resources: z.array(resourceSchema) }),
});
const detailSchema = workspaceSchema.extend({
  accounts: z.array(z.object({ account_ref: z.string(), platform: z.enum(["meta", "google"]), external_account_id: z.string(), currency: z.string(), status: z.string() })),
  campaigns: z.array(z.object({ draft: campaignDraftSchema,
    proposal: z.object({ id: z.string(), state: z.string(), diff_hash: z.string(), expires_at: z.string() }).nullable(),
    execution: z.object({ id: z.string(), outcome: z.string(), error_code: z.string().nullable(), entity_ref: z.string(), created_external_id: z.string().nullable(), applied_value: z.unknown() }).nullable(),
    step: z.object({ state: z.string(), label: z.string(), authorizes_spend: z.literal(false) }),
  })),
  campaigns_has_more: z.boolean(),
  activity: z.array(z.object({ seq: z.number(), actor: z.string(), kind: z.string(), payload: z.record(z.unknown()), occurred_at: z.string() })),
  runtime_jobs: z.array(z.object({ id: z.string(), state: z.string(), message: z.string(), updated_at: z.string() })),
  capabilities: z.object({ shared_context: z.boolean(), prepare_paused_proposal: z.boolean(), automatic_activation: z.boolean(), runtime_role: z.string(), budget_is_enforced_cap: z.boolean() }),
});
export type Workspace = z.infer<typeof detailSchema>;
export type WorkspaceResource = z.infer<typeof resourceSchema>;
export function useWorkspaces(businessId: string) {
  return useQuery({ queryKey: ["workspaces", businessId], enabled: Boolean(businessId), queryFn: () => apiClient.get("/workspaces", z.object({ items: z.array(workspaceSchema.extend({ campaign_count: z.number() })), has_more: z.boolean(), contract_version: z.literal(1) }), { business_id: businessId }), refetchInterval: 15000 });
}
export function useWorkspace(businessId: string, id: string) {
  return useQuery({ queryKey: ["workspace", businessId, id], enabled: Boolean(businessId && id), queryFn: () => apiClient.get(`/workspaces/${id}`, detailSchema, { business_id: businessId }), refetchInterval: 5000 });
}
export function useWorkspaceCommand(businessId: string) {
  const client = useQueryClient();
  return useMutation({ retry: false,
    mutationFn: ({ path, body }: { path: string; body: Record<string, unknown> }) => apiClient.post(`/workspaces${path}`, z.unknown(), body, { business_id: businessId }),
    onSuccess: async () => { await Promise.all(["workspaces", "workspace", "campaign-drafts", "proposals"].map(key => client.invalidateQueries({ queryKey: [key, businessId] }))); },
  });
}

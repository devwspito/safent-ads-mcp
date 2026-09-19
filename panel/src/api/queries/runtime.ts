import { useQuery } from "@tanstack/react-query";
import { z } from "zod";
import { apiClient } from "@/api/client";

export const runtimeJobSchema = z.object({
  id: z.string(), slug: z.string(), revision: z.string(),
  state: z.enum(["queued", "running", "prepared", "blocked", "failed", "cancelled"]),
  attempts: z.number(), message: z.string(), created_at: z.string(), updated_at: z.string(),
  lease_until: z.string().nullable(), authorizes_spend: z.literal(false),
  result: z.object({ summary: z.string(), blockers: z.array(z.string()), draft_id: z.string().nullable(), draft_revision: z.number().nullable(), published: z.literal(false), activation_blockers: z.array(z.string()) }).nullable(),
});
export type RuntimeJob = z.infer<typeof runtimeJobSchema>;
const connectionsSchema = z.object({ items: z.array(z.object({ id: z.string(), label: z.string(), runtime: z.enum(["codex", "claude"]), connected: z.boolean(), expires_at: z.string(), revoked_at: z.string().nullable(), last_seen_at: z.string().nullable() })) });
export const connectionTokenSchema = z.object({ id: z.string(), token: z.string(), expires_in_days: z.number() });

export function useRuntimeConnections(businessId: string) {
  return useQuery({ queryKey: ["runtime-connections", businessId], enabled: Boolean(businessId), queryFn: () => apiClient.get("/runtime/connections", connectionsSchema, { business_id: businessId }), refetchInterval: 15000, retry: false });
}

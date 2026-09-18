import { useQuery } from "@tanstack/react-query";
import { z } from "zod";
import { apiClient } from "@/api/client";

const slotSchema = z.object({ id: z.string(), title: z.string(), format: z.string(), copy: z.string(), uploaded: z.boolean() });
export const reviewSchema = z.object({ approved: z.boolean(), approved_at: z.string().nullable() });
const planSchema = z.object({ slug: z.string(), title: z.string(), summary: z.string(), proposal_id: z.string().nullable(), landing_url: z.string(), blockers: z.array(z.string()), documents: z.array(z.object({ title: z.string(), text: z.string() })), video_slots: z.array(slotSchema), revision: z.string(), review: reviewSchema });
const schema = z.object({ items: z.array(planSchema) });
export type LaunchPlan = z.infer<typeof planSchema>;

export function useLaunchPlans(businessId: string) {
  return useQuery({ queryKey: ["launch-plans", businessId], enabled: Boolean(businessId), queryFn: () => apiClient.get("/launch-plans", schema, { business_id: businessId }), retry: false });
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { apiClient } from "@/api/client";
import { acknowledgedSchema } from "@/api/schemas/proposals";
import {
  autonomyGateSchema,
  guardrailSchema,
  guardrailsResponseSchema,
  ruleSchema,
  rulesResponseSchema,
  type AutonomyGate,
  type AutonomyGateConfirmationInput,
  type GuardrailUpdate,
  type Rule,
} from "@/api/schemas/rules";

export function useRules(businessId: string) {
  return useQuery({
    queryKey: ["rules", businessId],
    queryFn: () => apiClient.get("/rules", rulesResponseSchema, { business_id: businessId }),
    enabled: Boolean(businessId),
  });
}

/** `PUT /rules/{id}` exige el cuerpo completo (rest-api.md); se parte de la regla vigente. */
export function useUpdateRule(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (input: { rule: Rule; patch: Partial<Pick<Rule, "is_enabled" | "autonomy_level" | "magnitude_pct">> }) =>
      apiClient.put(`/rules/${input.rule.rule_id}`, ruleSchema, { ...input.rule, ...input.patch }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["rules", businessId] }),
  });
}

export function useGuardrails(businessId: string) {
  return useQuery({
    queryKey: ["guardrails", businessId],
    queryFn: () => apiClient.get("/guardrails", guardrailsResponseSchema, { scope_ref: businessId }),
    enabled: Boolean(businessId),
  });
}

export function useUpdateGuardrail(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (input: { guardrailId: string; update: GuardrailUpdate }) =>
      apiClient.put(`/guardrails/${encodeURIComponent(input.guardrailId)}`, guardrailSchema, input.update, { business_id: businessId }),
    onSuccess: async () => { await Promise.all(["guardrails", "guardrails-setup"].map(key => queryClient.invalidateQueries({ queryKey: [key, businessId] }))); },
  });
}

export function useGuardrailSetup(businessId: string) {
  return useQuery({
    queryKey: ["guardrails-setup", businessId], enabled: Boolean(businessId),
    queryFn: () => apiClient.get("/guardrails/setup", z.object({ items: z.array(z.object({ account_ref: z.string(), platform: z.string(), platform_account_id: z.string(), currency: z.string(), guardrail: guardrailSchema.nullable() })) }), { business_id: businessId }),
  });
}

export function useAutonomyGate(businessId: string) {
  return useQuery<AutonomyGate>({
    queryKey: ["autonomy-gate", businessId],
    queryFn: () => apiClient.get("/rules/autonomy-gate", autonomyGateSchema, { business_id: businessId }),
    enabled: Boolean(businessId),
  });
}

/** Explicit owner confirmation bound to the exact request, never an OTP. */
export function useConfirmAutonomyGate(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: ({ confirmationToken, ...input }: AutonomyGateConfirmationInput & { confirmationToken?: string }) =>
      apiClient.post("/rules/autonomy-gate/confirmations", acknowledgedSchema, input, undefined, {
        ...(confirmationToken ? { "X-Action-Confirmation": confirmationToken } : {}),
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["autonomy-gate", businessId] }),
  });
}

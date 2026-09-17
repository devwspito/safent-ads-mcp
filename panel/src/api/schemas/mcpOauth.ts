/**
 * `contracts/oauth.md` §5 (API de consentimiento) y `tasks.md` T015b
 * (Enmiendas, "Agentes conectados"). `client_id` no aparece en el ejemplo de §5 pero T017+ y el
 * riesgo residual R-6 del threat-model exigen mostrarlo íntegro en la pantalla de consentimiento
 * — se añade aquí como campo requerido; hay que conciliarlo con `oauth.md` en la ola de backend.
 */
import { z } from "zod";

export const mcpOauthScopeNameSchema = z.enum(["ads:read", "ads:propose"]);
export type McpOauthScopeName = z.infer<typeof mcpOauthScopeNameSchema>;

export const mcpOauthConsentScopeSchema = z.object({
  name: mcpOauthScopeNameSchema,
  label: z.string(),
});
export type McpOauthConsentScope = z.infer<typeof mcpOauthConsentScopeSchema>;

export const mcpOauthConsentSchema = z.object({
  txn_id: z.string(),
  client_id: z.string(),
  client_name: z.string(),
  redirect_host: z.string(),
  scopes: z.array(mcpOauthConsentScopeSchema),
  expires_at: z.string(),
});
export type McpOauthConsent = z.infer<typeof mcpOauthConsentSchema>;

export const mcpOauthConsentActionResponseSchema = z.object({
  redirect_to: z.string(),
});
export type McpOauthConsentActionResponse = z.infer<typeof mcpOauthConsentActionResponseSchema>;

export const mcpOauthGrantSchema = z.object({
  grant_id: z.string(),
  client_id: z.string(),
  client_name: z.string(),
  redirect_host: z.string(),
  scopes: z.array(mcpOauthScopeNameSchema),
  created_at: z.string(),
  /** `application/list_grants.py`: `None` cuando el grant no tiene refresh token activo. */
  expires_at: z.string().nullable(),
  last_used_at: z.string().nullable(),
});
export type McpOauthGrant = z.infer<typeof mcpOauthGrantSchema>;

export const mcpOauthGrantsResponseSchema = z.object({
  grants: z.array(mcpOauthGrantSchema),
});

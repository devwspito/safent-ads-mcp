/** `contracts/rest-api.md` §Conexiones → Credenciales de desarrollador (owner decision, app-credentials-ui). */
import { z } from "zod";
import { platformSchema } from "@/api/schemas";

export const platformAppStatusSchema = z.object({
  platform: platformSchema,
  configured: z.boolean(),
  client_id_masked: z.string().nullable(),
  login_customer_id_masked: z.string().nullable(),
  redirect_uri: z.string(),
  updated_at: z.string().nullable(),
  client_type: z.enum(["web", "desktop"]).nullable().optional(),
});
export type PlatformAppStatus = z.infer<typeof platformAppStatusSchema>;

export const platformAppsResponseSchema = z.object({
  items: z.array(platformAppStatusSchema),
});

export interface SetGoogleAppCredentialsInput {
  client_id: string;
  client_secret?: string;
  client_type?: "web" | "desktop";
  login_customer_id?: string;
}

export interface SetMetaAppCredentialsInput {
  app_id: string;
  app_secret: string;
}

/** `GET/POST/DELETE /api/v1/integrations/cloudflare*` (lane 006-cloudflare-ui,
 * owner decision: "crear una conexion con Cloudflare pidiendo el token e
 * indicando el enlace donde crearlo"). */
import { z } from "zod";

export const cloudflareConnectionStatusSchema = z.object({
  connected: z.boolean(),
  account_id: z.string().nullable(),
  zones: z.array(z.string()),
  create_token_url: z.string(),
  required_permissions: z.array(z.string()),
  connected_at: z.string().nullable(),
});
export type CloudflareConnectionStatus = z.infer<typeof cloudflareConnectionStatusSchema>;

export interface ConnectCloudflareTokenInput {
  token: string;
  account_id?: string;
}

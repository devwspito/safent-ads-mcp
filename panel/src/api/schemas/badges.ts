/** `GET /badges` — contadores para la barra lateral, caché 30 s (rest-api.md §Auditoría y salud). */
import { z } from "zod";
import { brakeModeSchema } from "@/api/schemas";

export const badgesResponseSchema = z.object({
  proposals: z.object({ pending: z.number().int(), critical: z.number().int(), deferred: z.number().int() }),
  creatives: z.object({ pending_approval: z.number().int() }),
  signals: z.object({ new_since: z.number().int(), since: z.string() }),
  connections: z.object({ level: z.enum(["ok", "warn", "error"]), reason: z.string().nullable() }),
  brake: z.object({ engaged: z.boolean(), mode: brakeModeSchema.nullable() }),
});
export type BadgesResponse = z.infer<typeof badgesResponseSchema>;

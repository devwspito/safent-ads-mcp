/** `contracts/rest-api.md` §Conexiones, Telegram y ajustes — `/settings`, reconciled v2. */
import { z } from "zod";

export const themeSchema = z.enum(["light", "dark", "system"]);
export type Theme = z.infer<typeof themeSchema>;

export const activeHoursSchema = z.object({ start: z.string(), end: z.string() });
export type ActiveHours = z.infer<typeof activeHoursSchema>;

/** `timezone`/`currency` los dicta la plataforma (data-model §PlatformAccount): se muestran, no se editan. */
export const settingsSchema = z.object({
  business_id: z.string(),
  timezone: z.string(),
  currency: z.string().length(3),
  active_hours: activeHoursSchema,
  digest_hour: z.string(),
  /** Se guarda por `owner_id`, no por negocio. */
  theme: themeSchema,
});
export type Settings = z.infer<typeof settingsSchema>;

export const settingsUpdateSchema = z.object({
  active_hours: activeHoursSchema,
  digest_hour: z.string(),
  theme: themeSchema,
});
export type SettingsUpdate = z.infer<typeof settingsUpdateSchema>;

/** `contracts/rest-api.md` §Conexiones, Telegram y ajustes (US1/US2), reconciled v2. */
import { z } from "zod";
import { platformSchema } from "@/api/schemas";

export const platformAccountStatusSchema = z.enum(["ACTIVE", "THROTTLED", "SUSPENDED", "READ_ONLY"]);
export type PlatformAccountStatus = z.infer<typeof platformAccountStatusSchema>;

export const tokenHealthSchema = z.enum(["ok", "expiring_soon", "expired", "revoked"]);

export const platformAccountTokenSchema = z.object({
  health: tokenHealthSchema,
  expires_at: z.string().nullable(),
  checked_at: z.string(),
});

export const platformAccountQuotaSchema = z.object({
  window: z.string(),
  used_pct: z.number().nullable(),
  writes_remaining: z.number().int().nullable(),
});

export const unavailableLeverSchema = z.object({
  code: z.string(),
  label: z.string(),
  reason: z.string(),
});
export type UnavailableLever = z.infer<typeof unavailableLeverSchema>;

export const platformAccountSchema = z.object({
  platform_account_id: z.string(),
  platform: platformSchema,
  external_account_id: z.string(),
  label: z.string(),
  status: platformAccountStatusSchema,
  currency: z.string().length(3),
  timezone: z.string(),
  api_tier: z.string(),
  token: platformAccountTokenSchema,
  quota: platformAccountQuotaSchema,
  unavailable_levers: z.array(unavailableLeverSchema),
  last_synced_at: z.string().nullable(),
  last_error_code: z.string().nullable(),
});
export type PlatformAccount = z.infer<typeof platformAccountSchema>;

export const platformAccountsResponseSchema = z.object({
  items: z.array(platformAccountSchema),
});

export const reconnectStartResponseSchema = z.object({
  session_id: z.string(),
  authorize_url: z.string(),
  expires_at: z.string(),
});

export const reconnectStateSchema = z.enum(["waiting", "ok", "error"]);

export const reconnectStatusResponseSchema = z.object({
  state: reconnectStateSchema,
  error_code: z.string().nullable(),
  message: z.string().nullable(),
});

/** `POST /platform-accounts/meta/system-user-token` (201) — nunca lleva el token pegado ni cambiado. */
export const connectedAccountSummarySchema = z.object({
  platform: platformSchema,
  external_account_id: z.string(),
  label: z.string(),
  currency: z.string().length(3),
  timezone: z.string(),
  api_tier: z.string(),
});
export type ConnectedAccountSummary = z.infer<typeof connectedAccountSummarySchema>;

export const metaSystemUserTokenResponseSchema = z.object({
  accounts: z.array(connectedAccountSummarySchema),
});

export const telegramPairingStatusSchema = z.enum(["unpaired", "pending", "paired"]);

export const telegramPairingSchema = z.object({
  status: telegramPairingStatusSchema,
  chat_id_masked: z.string().nullable(),
  paired_at: z.string().nullable(),
  allowlist_configured: z.boolean(),
  pairing_code: z.string().nullable(),
  code_expires_at: z.string().nullable(),
});
export type TelegramPairing = z.infer<typeof telegramPairingSchema>;

export const telegramPairingStartResponseSchema = z.object({
  pairing_code: z.string(),
  code_expires_at: z.string(),
});

export const telegramTestMessageResponseSchema = z.object({
  notification_id: z.string(),
});

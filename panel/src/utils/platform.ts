import type { Platform } from "@/api/schemas";
import type { PlatformAccount } from "@/api/schemas/connections";

const labels: Record<Platform, string> = {
  google: "Google Ads",
  meta: "Meta Ads",
};

export function platformLabel(platform: Platform): string {
  return labels[platform];
}

const shortLabels: Record<Platform, string> = {
  google: "Google",
  meta: "Meta",
};

/** Forma corta para la fila de propuesta — panel-interaction-spec.md §2.2 ("campaña · plataforma · cuenta"). */
export function platformShortLabel(platform: Platform): string {
  return shortLabels[platform];
}

/**
 * Nombre de cuenta sin repetir la plataforma — design.md §3 fix (a): la fila ya muestra
 * `platformShortLabel` por su lado, así que un `account_label` del tipo "Google Ads — Cuenta
 * Principal" (contrato de hoy) duplicaría la plataforma. Quita ese prefijo cuando coincide;
 * si el servidor ya manda el nombre limpio, lo deja tal cual.
 */
export function accountDisplayName(platform: Platform, accountLabel: string | null | undefined): string | null {
  if (!accountLabel) return null;
  const prefix = `${platformLabel(platform)} — `;
  return accountLabel.startsWith(prefix) ? accountLabel.slice(prefix.length) : accountLabel;
}

/**
 * `/platform-accounts` todavía no persiste un nombre elegible (`platform_accounts.label`,
 * data-model.md) — hoy `label` es literalmente `str(account_ref)` (`list_platform_accounts.py`),
 * la misma referencia cruda que `platform_account_id`. Detectar exactamente esa igualdad, nunca
 * adivinar por formato, y caer a «Cuenta <external_account_id>» — jamás la referencia cruda.
 */
export function platformAccountDisplayName(account: Pick<PlatformAccount, "platform" | "label" | "platform_account_id" | "external_account_id">): string {
  const friendly = account.label === account.platform_account_id ? `Cuenta ${account.external_account_id}` : account.label;
  return accountDisplayName(account.platform, friendly) ?? friendly;
}

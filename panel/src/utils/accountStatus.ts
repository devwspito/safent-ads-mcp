import type { PlatformAccount, PlatformAccountStatus } from "@/api/schemas/connections";

export type AccountRowState =
  | { kind: "active" }
  | { kind: "brake" }
  | { kind: "reconnect"; reason: string }
  | { kind: "read_only"; label: string };

const STATUS_LABEL: Record<PlatformAccountStatus, string> = {
  ACTIVE: "Activa",
  THROTTLED: "Limitada por la plataforma",
  SUSPENDED: "Suspendida por la plataforma",
  READ_ONLY: "Solo lectura",
};

/**
 * Estado de cuenta en palabras llanas — design.md §4.3/§10.5: una sola cabecera por cuenta,
 * nunca repetida en cada campaña. Prioridad: reconectar (sin token no hay nada que hacer) >
 * solo lectura/limitada/suspendida (la plataforma lo impide) > cambios parados (reversible con
 * un clic) > activa.
 */
export function accountRowState(account: PlatformAccount, brakeEngaged: boolean): AccountRowState {
  if (account.token.health === "expired") return { kind: "reconnect", reason: "Conexión caducada — reconecta" };
  if (account.token.health === "revoked") return { kind: "reconnect", reason: "Desconectada" };
  if (account.status !== "ACTIVE") return { kind: "read_only", label: STATUS_LABEL[account.status] };
  if (brakeEngaged) return { kind: "brake" };
  return { kind: "active" };
}

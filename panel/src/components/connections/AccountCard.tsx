import { useState } from "react";
import { ApiRequestError } from "@/api/client";
import { useRevokePlatformAccount } from "@/api/queries/connections";
import type { PlatformAccount } from "@/api/schemas/connections";
import { TypedConfirmDialog } from "@/components/common/TypedConfirmDialog";
import { platformLabel } from "@/utils/platform";
import { formatRelativeTime } from "@/utils/time";
import styles from "./AccountCard.module.css";

const STATUS_LABELS: Record<PlatformAccount["status"], string> = {
  ACTIVE: "Activa",
  THROTTLED: "Limitada",
  SUSPENDED: "Suspendida",
  READ_ONLY: "Solo lectura",
};

const STATUS_CLASS: Record<PlatformAccount["status"], string> = {
  ACTIVE: styles.statusActive ?? "",
  THROTTLED: styles.statusReadOnly ?? "",
  READ_ONLY: styles.statusReadOnly ?? "",
  SUSPENDED: styles.statusSuspended ?? "",
};

const TOKEN_HEALTH_LABELS: Record<string, string> = {
  ok: "Token en orden",
  expiring_soon: "Token caduca pronto",
  expired: "Token caducado",
  revoked: "Token revocado",
};

const DEFAULT_REVOKE_ERROR = "No se pudo revocar la conexión.";

interface AccountCardProps {
  account: PlatformAccount;
  businessId: string;
}

/**
 * Tarjeta por cuenta: plataforma, estado, salud del token, cuota, palancas no disponibles,
 * revocar con confirmación tecleada — panel-interaction-spec.md §3.8. "Conectar"/"Reconectar"
 * vive por plataforma en `ConnectProviderCard`, no aquí: la autorización OAuth es del
 * propietario, no de una cuenta concreta (rest-api.md §Conexiones).
 */
export function AccountCard({ account, businessId }: AccountCardProps) {
  const [confirmingRevoke, setConfirmingRevoke] = useState(false);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const revoke = useRevokePlatformAccount(businessId);

  async function handleRevoke() {
    setConfirmingRevoke(false);
    setRevokeError(null);
    try {
      await revoke.mutateAsync(account.platform_account_id);
    } catch (error) {
      setRevokeError(error instanceof ApiRequestError ? error.message : DEFAULT_REVOKE_ERROR);
    }
  }

  const isRevoked = account.token.health === "revoked";

  return (
    <div className={styles.card}>
      <div className={styles.header}>
        <span className={styles.label}>{account.label}</span>
        <span className={`${styles.status} ${STATUS_CLASS[account.status]}`}>{STATUS_LABELS[account.status]}</span>
      </div>

      <div className={styles.grid}>
        <div className={styles.field}>
          <span className={styles.fieldLabel}>Plataforma</span>
          <span className={styles.fieldValue}>{platformLabel(account.platform)}</span>
        </div>
        <div className={styles.field}>
          <span className={styles.fieldLabel}>Última sincronización</span>
          <span className={styles.fieldValue}>{account.last_synced_at ? formatRelativeTime(account.last_synced_at) : "—"}</span>
        </div>
      </div>

      <details className={styles.technicalDetails}>
        <summary>Ver detalles técnicos</summary>
        <div className={styles.grid}>
          <div className={styles.field}>
            <span className={styles.fieldLabel}>Token</span>
            <span className={styles.fieldValue}>
              {TOKEN_HEALTH_LABELS[account.token.health] ?? account.token.health}
              {account.token.expires_at ? ` · caduca ${formatRelativeTime(account.token.expires_at)}` : ""}
            </span>
          </div>
          <div className={styles.field}>
            <span className={styles.fieldLabel}>Nivel de acceso</span>
            <span className={styles.fieldValue}>{account.api_tier}</span>
          </div>
          <div className={styles.field}>
            <span className={styles.fieldLabel}>Cuota usada</span>
            <span className={styles.fieldValue}>{account.quota.used_pct !== null ? `${account.quota.used_pct} %` : "—"}</span>
          </div>
        </div>
        {account.unavailable_levers.length > 0 ? (
          <span className={styles.levers}>No disponible: {account.unavailable_levers.map((lever) => lever.label).join(", ")}</span>
        ) : null}
        {account.last_error_code ? <span className={styles.levers}>Último error: {account.last_error_code}</span> : null}
      </details>

      <div className={styles.footer}>
        <button
          type="button"
          className={styles.revokeButton}
          onClick={() => setConfirmingRevoke(true)}
          disabled={isRevoked || revoke.isPending}
        >
          {isRevoked ? "Quitada" : "Quitar"}
        </button>
        {revokeError ? (
          <span className={styles.revokeError} role="alert">
            {revokeError}
          </span>
        ) : null}
      </div>

      {confirmingRevoke ? (
        <TypedConfirmDialog
          title={`Quitar ${account.label}`}
          description="Esta cuenta dejará de operarse hasta volver a conectar la plataforma desde cero. Escribe QUITAR para confirmar."
          confirmLabel="Quitar"
          confirmWord="QUITAR"
          danger
          onConfirm={() => void handleRevoke()}
          onClose={() => setConfirmingRevoke(false)}
        />
      ) : null}
    </div>
  );
}

import { Link } from "react-router-dom";
import type { PlatformAccount } from "@/api/schemas/connections";
import type { TickerRow } from "@/api/schemas/cockpit";
import type { AccountRowState } from "@/utils/accountStatus";
import { platformAccountDisplayName } from "@/utils/platform";
import { CampaignDetail } from "./CampaignDetail";
import { CampaignRow, type CampaignRowActions } from "./CampaignRow";
import styles from "./AccountGroup.module.css";

interface AccountGroupProps {
  account: PlatformAccount;
  state: AccountRowState;
  rows: TickerRow[];
  expandedEntityRef: string | null;
  actionBusyByRef: Record<string, "pausing" | "resuming" | undefined>;
  /** "Datos de hace N h." — se aplica a toda cuenta que no tenga ya un motivo propio más fuerte. */
  staleReason: string | null;
  onResumeAccount: () => void;
  actions: CampaignRowActions;
}

const ACTIVE_STATUSES = new Set(["ACTIVE"]);

/** Cabecera de cuenta — design.md §4.3: el motivo se dice una vez aquí, nunca repetido por fila. */
export function AccountGroup({ account, state, rows, expandedEntityRef, actionBusyByRef, staleReason, onResumeAccount, actions }: AccountGroupProps) {
  const activeCount = rows.filter((row) => ACTIVE_STATUSES.has(row.status)).length;
  const displayName = platformAccountDisplayName(account);
  const disabledReason =
    state.kind === "brake" ? "Los cambios de esta cuenta están parados."
    : state.kind === "reconnect" ? state.reason
    : state.kind === "read_only" ? state.label
    : staleReason;

  return (
    <section className={styles.group}>
      <div className={styles.header}>
        <span className={styles.headerLabel}>
          {displayName}
          {state.kind === "active" ? null : <span className={styles.headerState}> · {state.kind === "brake" ? "Cambios parados en esta cuenta" : state.kind === "reconnect" ? state.reason : state.label}</span>}
          {state.kind === "active" ? <span className={styles.headerState}> · Activa</span> : null}
        </span>
        {state.kind === "reconnect" ? (
          <Link className={styles.headerLink} to="/ajustes">
            Arreglarlo en Ajustes
          </Link>
        ) : state.kind === "brake" ? (
          <button type="button" className={styles.resumeButton} onClick={onResumeAccount}>
            Reanudar los cambios
          </button>
        ) : (
          <span className={styles.headerCount}>
            {rows.length} {rows.length === 1 ? "campaña" : "campañas"} · {activeCount} {activeCount === 1 ? "activa" : "activas"}
          </span>
        )}
      </div>

      {rows.map((row) => (
        <div key={row.entity_ref}>
          <CampaignRow
            row={row}
            accountName={displayName}
            isExpanded={expandedEntityRef === row.entity_ref}
            accountDisabledReason={disabledReason}
            actionBusy={actionBusyByRef[row.entity_ref] ?? null}
            actions={actions}
          />
          {expandedEntityRef === row.entity_ref ? <CampaignDetail entityRef={row.entity_ref} /> : null}
        </div>
      ))}
    </section>
  );
}

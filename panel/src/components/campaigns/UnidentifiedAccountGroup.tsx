import type { TickerRow } from "@/api/schemas/cockpit";
import { CampaignDetail } from "./CampaignDetail";
import { CampaignRow, type CampaignRowActions } from "./CampaignRow";
import styles from "./AccountGroup.module.css";

interface UnidentifiedAccountGroupProps {
  rows: TickerRow[];
  expandedEntityRef: string | null;
  actionBusyByRef: Record<string, "pausing" | "resuming" | undefined>;
  actions: CampaignRowActions;
}

/**
 * Hotfix 0.2.20 Bug C: una fila cuyo `platform_account_id` no coincide con
 * ninguna cuenta de `/platform-accounts` (formatos históricamente
 * distintos entre `/portfolio` y `/platform-accounts`) se mostraba
 * silenciosamente cero veces. Mejor una cuenta "no identificada" y visible
 * que una campaña que desaparece del panel.
 */
export function UnidentifiedAccountGroup({ rows, expandedEntityRef, actionBusyByRef, actions }: UnidentifiedAccountGroupProps) {
  if (rows.length === 0) return null;

  return (
    <section className={styles.group}>
      <div className={styles.header}>
        <span className={styles.headerLabel}>Cuenta no identificada</span>
        <span className={styles.headerCount}>
          {rows.length} {rows.length === 1 ? "campaña" : "campañas"}
        </span>
      </div>

      {rows.map((row) => (
        <div key={row.entity_ref}>
          <CampaignRow
            row={row}
            accountName="Cuenta no identificada"
            isExpanded={expandedEntityRef === row.entity_ref}
            accountDisabledReason={null}
            actionBusy={actionBusyByRef[row.entity_ref] ?? null}
            actions={actions}
          />
          {expandedEntityRef === row.entity_ref ? <CampaignDetail entityRef={row.entity_ref} /> : null}
        </div>
      ))}
    </section>
  );
}

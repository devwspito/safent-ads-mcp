import type { Measure, TickerRow } from "@/api/schemas/cockpit";
import { RowMenu } from "@/components/common/RowMenu";
import { PlatformBadge } from "@/components/common/PlatformBadge";
import { entityCapabilities } from "@/utils/entityCapabilities";
import { plainEntityStatusLabel } from "@/utils/entityStatus";
import { formatCockpitMoney } from "@/utils/money";
import styles from "./CampaignRow.module.css";

const STATUS_CHIP_CLASS: Record<string, string> = {
  ACTIVE: "statusActive",
  PAUSED: "statusPaused",
  REMOVED: "statusRemoved",
};

function StatusChip({ status }: { status: string }) {
  const variant = STATUS_CHIP_CLASS[status] ?? "statusRemoved";
  return <span className={`${styles.statusChip} ${styles[variant]}`}>{plainEntityStatusLabel(status)}</span>;
}

const MISSING_DATA = "Faltan datos de esta cuenta";

function measureText<T>(measure: Measure<T>, format: (value: T) => string): string {
  return measure.status === "available" ? format(measure.value) : MISSING_DATA;
}

function warningLine(row: Pick<TickerRow, "learning_state" | "is_controllable" | "signal">): string | null {
  if (row.learning_state.is_learning) return "Aprendiendo: Safent espera unos días antes de tocarla.";
  if (!row.is_controllable) return "Sin control desde aquí.";
  return row.signal?.cause ?? null;
}

export interface CampaignRowActions {
  onToggleExpand: (entityRef: string) => void;
  onPause: (row: TickerRow) => void;
  onResume: (row: TickerRow) => void;
  onRequestDelete: (row: TickerRow) => void;
}

interface CampaignRowProps {
  row: TickerRow;
  accountName: string;
  isExpanded: boolean;
  /** Motivo de la cuenta (freno, solo lectura, reconectar) — se dice una vez en la cabecera y se hereda aquí. */
  accountDisabledReason: string | null;
  actionBusy: "pausing" | "resuming" | null;
  actions: CampaignRowActions;
}

/** Fila de campaña — design.md §4.1, gramática de §2.2. */
export function CampaignRow({ row, accountName, isExpanded, accountDisabledReason, actionBusy, actions }: CampaignRowProps) {
  const status = row.status;
  const caps = entityCapabilities(status, row.is_controllable);
  const warning = warningLine(row);
  const busy = actionBusy !== null;

  const actionDisabled = Boolean(accountDisabledReason) || busy || (status === "ACTIVE" ? !caps.canPause : status === "PAUSED" ? !caps.canResume : true);
  const actionDisabledReason = accountDisabledReason
    ?? (row.learning_state.is_learning ? "Aprendiendo: Safent espera unos días antes de tocarla."
      : !row.is_controllable ? "No se puede cambiar desde aquí."
      : status !== "ACTIVE" && status !== "PAUSED" ? "No se puede cambiar desde aquí."
      : null);
  const reasonId = `campaign-action-reason-${row.entity_ref}`;

  const canDeleteHere = caps.canDelete && !accountDisabledReason;

  return (
    <div className={styles.row} data-entity-ref={row.entity_ref}>
      <div
        className={`${styles.content} pressable-row`}
        onClick={() => actions.onToggleExpand(row.entity_ref)}
        role="button"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          actions.onToggleExpand(row.entity_ref);
        }}
      >
        <span className={styles.name}>{row.name}</span>
        <span className={styles.where}>
          <PlatformBadge platform={row.platform} />
          <span>{accountName}</span>
          <StatusChip status={status} />
        </span>
        {warning ? <span className={styles.warning}>{warning}</span> : null}
      </div>

      <span className={styles.figures}>
        {row.cap ? `${formatCockpitMoney(row.cap)}/día · tope` : "Sin tope"}
        <br />
        {formatCockpitMoney(row.spend)} hoy
        <br />
        {row.leads.status !== "available" && row.cost_per_lead.actual.status !== "available" ? (
          MISSING_DATA
        ) : (
          <>
            {measureText(row.leads, (v) => `${v} leads`)}<span className={styles.figureSep}>·</span>
            {measureText(row.cost_per_lead.actual, (v) => `${formatCockpitMoney(v)}/lead`)}
          </>
        )}
      </span>

      <div className={styles.actions}>
        <button
          type="button"
          className={styles.primaryButton}
          disabled={actionDisabled}
          aria-busy={busy || undefined}
          aria-describedby={actionDisabled && actionDisabledReason ? reasonId : undefined}
          onClick={() => (status === "PAUSED" ? actions.onResume(row) : actions.onPause(row))}
        >
          {actionBusy === "pausing" ? "Pausando…" : actionBusy === "resuming" ? "Reanudando…" : status === "PAUSED" ? "Reanudar" : "Pausar"}
        </button>
        {actionDisabled && actionDisabledReason ? <span id={reasonId} className={styles.disabledReason}>{actionDisabledReason}</span> : null}
        <div className={styles.secondaryRow}>
          <RowMenu
            label={`Más opciones para ${row.name}`}
            items={[
              { key: "delete", label: "Eliminar", danger: true, disabled: !canDeleteHere, onSelect: () => actions.onRequestDelete(row) },
              { key: "history", label: "Ver historial", onSelect: () => actions.onToggleExpand(row.entity_ref) },
            ]}
          />
        </div>
        <button type="button" className={styles.detailToggle} aria-expanded={isExpanded} aria-label={isExpanded ? "Colapsar" : `Ver detalle de ${row.name}`} onClick={() => actions.onToggleExpand(row.entity_ref)}>
          {isExpanded ? "Colapsar ⌃" : "Detalle ⌄"}
        </button>
      </div>
    </div>
  );
}

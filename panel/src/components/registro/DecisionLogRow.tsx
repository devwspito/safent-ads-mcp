import { useNavigate } from "react-router-dom";
import type { DecisionLogEntry } from "@/api/schemas/decisionLog";
import { formatExactDate, formatRelativeTime } from "@/utils/time";
import styles from "./DecisionLogRow.module.css";

const EVENT_LABELS: Record<string, string> = {
  SignalEmitted: "Señal emitida",
  ProposalRaised: "Propuesta creada",
  ProposalApproved: "Propuesta aprobada",
  ProposalRejected: "Propuesta rechazada",
  ProposalExpired: "Propuesta caducada",
  ExecutionSucceeded: "Cambio aplicado",
  ExecutionFailed: "Cambio fallido",
  ExecutionUndone: "Cambio deshecho",
  RuleFired: "Regla disparada",
  EmergencyBrakeEngaged: "Freno activado",
  EmergencyBrakeReleased: "Freno desactivado",
  PlatformAccountSuspended: "Cuenta suspendida",
  AdEntityDrifted: "Entidad desincronizada",
};

const ACTOR_LABELS: Record<string, string> = {
  owner: "Propietario",
  rule_engine: "Motor de reglas",
  agent: "Agente",
  system: "Sistema",
};

interface DecisionLogRowProps {
  entry: DecisionLogEntry;
  expanded: boolean;
  onToggle: () => void;
}

/** Fila de la bitácora — abre el antes y después, enlaza a su propuesta — panel-interaction-spec.md §3.7. */
export function DecisionLogRow({ entry, expanded, onToggle }: DecisionLogRowProps) {
  const navigate = useNavigate();
  const hasDiff = entry.before !== null || entry.after !== null;

  return (
    <>
      <div className={styles.row} onClick={onToggle} role="button" tabIndex={0} aria-expanded={expanded} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onToggle(); } }}>
        <span className={styles.seq}>#{entry.seq}</span>
        <span className={styles.eventType}>{EVENT_LABELS[entry.event_type] ?? entry.event_type}</span>
        <span className={styles.summary} title={entry.summary}>
          {entry.entity_name ? `${entry.entity_name} — ` : ""}
          {entry.summary}
        </span>
        <span className={styles.actor}>{ACTOR_LABELS[entry.actor_kind]} · {entry.actor_label}</span>
        <span className={styles.time} title={formatExactDate(entry.occurred_at)}>
          {formatRelativeTime(entry.occurred_at)}
        </span>
      </div>
      {expanded ? (
        <div className={styles.detail}>
          {hasDiff ? (
            <>
              <div className={styles.diffBlock}>
                <span className={styles.diffLabel}>Antes</span>
                <span className={styles.diffValue}>{entry.before ? JSON.stringify(entry.before, null, 2) : "—"}</span>
              </div>
              <div className={styles.diffBlock}>
                <span className={styles.diffLabel}>Después</span>
                <span className={styles.diffValue}>{entry.after ? JSON.stringify(entry.after, null, 2) : "—"}</span>
              </div>
            </>
          ) : (
            <span className={styles.diffValue}>Sin cambio de estado asociado.</span>
          )}
          {entry.proposal_id ? (
            <button
              type="button"
              className={styles.link}
              onClick={(e) => {
                e.stopPropagation();
                navigate(`/propuestas?business_id=${entry.business_id}`);
              }}
            >
              Ver propuesta relacionada
            </button>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

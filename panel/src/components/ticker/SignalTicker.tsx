import { useNavigate } from "react-router-dom";
import type { SignalRow } from "@/api/schemas";
import { SignalChip } from "@/components/signals/SignalChip";
import { formatMoney } from "@/utils/money";
import styles from "./SignalTicker.module.css";

interface SignalTickerProps {
  signals: SignalRow[];
}

/**
 * Señales desplazables por el usuario: no movimiento continuo ni botones duplicados.
 */
export function SignalTicker({ signals }: SignalTickerProps) {
  const navigate = useNavigate();

  const ordered = [...signals].sort((a, b) => b.money_at_stake.amount - a.money_at_stake.amount);

  return (
    <div className={styles.wrap} role="region" aria-label="Señales de hoy">
      <div className={styles.track}>
        {ordered.map((signal) => (
          <button
            key={signal.signal_id}
            type="button"
            className={styles.item}
            onClick={() => navigate(`/campanas/${encodeURIComponent(signal.entity_ref)}`)}
          >
            <span className={styles.name}>{signal.entity_name}</span>
            <SignalChip kind={signal.kind} strength={signal.strength} />
            <span className={styles.money}>{formatMoney(signal.money_at_stake)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

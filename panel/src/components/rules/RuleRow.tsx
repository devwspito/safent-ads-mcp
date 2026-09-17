import { useState } from "react";
import type { Rule } from "@/api/schemas/rules";
import type { RuleSwitchState } from "@/utils/rules";
import styles from "./RuleRow.module.css";

function switchStateFor(rule: Rule): RuleSwitchState {
  if (!rule.is_enabled) return "apagada";
  return rule.autonomy_level === "AUTO" ? "automatica" : "avisar";
}

interface RuleRowProps {
  rule: Rule;
  autonomyReady: boolean;
  autonomyBlockedReason: string | null;
  onChangeSwitch: (rule: Rule, state: RuleSwitchState) => void;
  onChangeMagnitude: (rule: Rule, magnitudePct: number) => void;
}

/** Fila del catálogo — conmutador Apagada/Avisar/Automática + umbral con rango seguro (panel-interaction-spec.md §3.6). */
export function RuleRow({ rule, autonomyReady, autonomyBlockedReason, onChangeSwitch, onChangeMagnitude }: RuleRowProps) {
  const current = switchStateFor(rule);
  const [magnitudeInput, setMagnitudeInput] = useState(String(rule.magnitude_pct));
  const magnitudeError = validateMagnitude(magnitudeInput);
  const autoDisabled = !autonomyReady && current !== "automatica";

  function commitMagnitude() {
    const value = Number(magnitudeInput);
    if (!magnitudeError && value !== rule.magnitude_pct) onChangeMagnitude(rule, value);
  }

  return (
    <div className={styles.row}>
      <div className={styles.name}>
        <span className={styles.nameText}>{rule.name}</span>
        <span className={styles.code}>{rule.code}</span>
      </div>
      <span className={styles.stat}>{rule.firings_30d} disparos/30 d</span>
      <span className={styles.stat}>{rule.hit_rate_pct !== null ? `${rule.hit_rate_pct.toString().replace(".", ",")} % acierto` : "Sin histórico"}</span>

      <div className={styles.magnitude}>
        <label className="visually-hidden" htmlFor={`magnitude-${rule.rule_id}`}>
          Magnitud del cambio (%)
        </label>
        <input
          id={`magnitude-${rule.rule_id}`}
          className={styles.magnitudeInput}
          type="number"
          min={1}
          max={100}
          value={magnitudeInput}
          aria-invalid={Boolean(magnitudeError)}
          onChange={(e) => setMagnitudeInput(e.target.value)}
          onBlur={commitMagnitude}
        />
        <span>%</span>
      </div>
      {magnitudeError ? <span className={styles.magnitudeError}>{magnitudeError}</span> : null}

      <div className={styles.switch} role="group" aria-label={`Autonomía de ${rule.name}`}>
        <button
          type="button"
          className={`${styles.switchOption} ${current === "apagada" ? styles.switchOptionActive : ""}`}
          onClick={() => onChangeSwitch(rule, "apagada")}
          aria-pressed={current === "apagada"}
        >
          Apagada
        </button>
        <button
          type="button"
          className={`${styles.switchOption} ${current === "avisar" ? styles.switchOptionActive : ""}`}
          onClick={() => onChangeSwitch(rule, "avisar")}
          aria-pressed={current === "avisar"}
        >
          Avisar
        </button>
        <button
          type="button"
          className={`${styles.switchOption} ${current === "automatica" ? styles.switchOptionActive : ""}`}
          disabled={autoDisabled}
          aria-pressed={current === "automatica"}
          title={autoDisabled ? (autonomyBlockedReason ?? "Autonomía deshabilitada") : undefined}
          onClick={() => onChangeSwitch(rule, "automatica")}
        >
          Automática
        </button>
      </div>
      {rule.autonomy_level === "APPROVAL" && current === "avisar" ? <span className={styles.approvalNote}>Exige aprobación</span> : null}
    </div>
  );
}

function validateMagnitude(value: string): string | null {
  const num = Number(value);
  if (value.trim() === "" || Number.isNaN(num)) return "Introduce un número";
  if (num < 1 || num > 100) return "Rango seguro: 1–100 %";
  return null;
}

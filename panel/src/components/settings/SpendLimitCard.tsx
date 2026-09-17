import { useId, useState } from "react";
import type { Guardrail, GuardrailUpdate } from "@/api/schemas/rules";
import styles from "./SpendLimitCard.module.css";

interface SpendLimitCardProps {
  guardrail: Guardrail;
  busy: boolean;
  justSaved: boolean;
  onSave: (update: GuardrailUpdate, requiresConfirm: boolean) => void;
}

/**
 * Límites de gasto en palabras llanas — design.md §6.2: solo Tope al día / Tope al mes, con el
 * importe en euros, el resto del guardarraíl técnico (suelo, techo, salto máximo…) se queda
 * intacto en `/ajustes/avanzado`. Bajar es instantáneo; subir cualquiera de los dos pide la
 * hoja tecleada SUBIR ya existente.
 */
export function SpendLimitCard({ guardrail, busy, justSaved, onSave }: SpendLimitCardProps) {
  const [daily, setDaily] = useState(String(guardrail.daily_cap));
  const [monthly, setMonthly] = useState(String(guardrail.monthly_cap));
  const idPrefix = useId();

  const dailyNum = Number(daily);
  const monthlyNum = Number(monthly);
  const dailyInvalid = daily.trim() === "" || !Number.isFinite(dailyNum) || dailyNum <= 0;
  const monthlyInvalid = monthly.trim() === "" || !Number.isFinite(monthlyNum) || monthlyNum <= 0;
  const invalid = dailyInvalid || monthlyInvalid;
  const raising = !invalid && (dailyNum > guardrail.daily_cap || monthlyNum > guardrail.monthly_cap);

  function handleSave() {
    if (invalid || busy) return;
    const update: GuardrailUpdate = { ...guardrail, daily_cap: dailyNum, monthly_cap: monthlyNum };
    onSave(update, raising);
  }

  return (
    <div className={styles.card}>
      <p className={styles.title}>{guardrail.scope_label}</p>
      <div className={styles.row}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${idPrefix}-daily`}>
            Tope al día
          </label>
          <div className={styles.inputGroup}>
            <input
              id={`${idPrefix}-daily`}
              className={styles.input}
              type="number"
              min="0"
              step="1"
              disabled={busy}
              value={daily}
              aria-invalid={dailyInvalid}
              onChange={(event) => setDaily(event.target.value)}
            />
            <span className={styles.currencySuffix} aria-hidden="true">€</span>
          </div>
          {dailyInvalid ? <span className={styles.error}>Introduce un número mayor que cero</span> : null}
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${idPrefix}-monthly`}>
            Tope al mes
          </label>
          <div className={styles.inputGroup}>
            <input
              id={`${idPrefix}-monthly`}
              className={styles.input}
              type="number"
              min="0"
              step="1"
              disabled={busy}
              value={monthly}
              aria-invalid={monthlyInvalid}
              onChange={(event) => setMonthly(event.target.value)}
            />
            <span className={styles.currencySuffix} aria-hidden="true">€</span>
          </div>
          {monthlyInvalid ? <span className={styles.error}>Introduce un número mayor que cero</span> : null}
        </div>
      </div>
      <div className={styles.footer}>
        <button type="button" className={styles.save} disabled={invalid || busy} onClick={handleSave}>
          {busy ? "Guardando…" : "Guardar"}
        </button>
        {raising ? <span className={styles.hint}>Subir un límite pedirá confirmación.</span> : null}
        {justSaved ? <span className={styles.saved}>Guardado.</span> : null}
      </div>
    </div>
  );
}

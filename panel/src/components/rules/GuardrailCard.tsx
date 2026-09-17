import { useId, useState } from "react";
import type { Guardrail, GuardrailUpdate } from "@/api/schemas/rules";
import styles from "./GuardrailCard.module.css";

interface FieldSpec {
  key: keyof GuardrailUpdate;
  label: string;
  suffix: string;
}

const FIELDS: FieldSpec[] = [
  { key: "daily_cap", label: "Tope diario", suffix: "€" },
  { key: "monthly_cap", label: "Tope mensual", suffix: "€" },
  { key: "budget_floor", label: "Suelo", suffix: "€" },
  { key: "budget_ceiling", label: "Techo", suffix: "€" },
  { key: "max_step_pct", label: "Salto máximo por cambio", suffix: "%" },
  { key: "max_changes_per_entity_per_day", label: "Cambios máximos por día", suffix: "" },
  { key: "min_viable_spend", label: "Gasto mínimo viable", suffix: "€" },
];

const RAISE_SENSITIVE: Array<keyof GuardrailUpdate> = ["daily_cap", "monthly_cap", "budget_ceiling"];

interface GuardrailCardProps {
  busy?: boolean;
  refreshing?: boolean;
  guardrail: Guardrail;
  onSave: (update: GuardrailUpdate, requiresConfirm: boolean) => void;
}

/** Guardarraíles por cuenta — bajar es inmediato, subir exige confirmación tecleada (panel-interaction-spec.md §3.6). */
export function GuardrailCard({ guardrail, onSave, busy = false, refreshing = false }: GuardrailCardProps) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(FIELDS.map((f) => [f.key, String(guardrail[f.key])])),
  );
  const idPrefix = useId();

  const errors = validate(values);
  const hasErrors = Object.keys(errors).length > 0;
  const raised = RAISE_SENSITIVE.some((key) => Number(values[key]) > guardrail[key]);

  function handleSave() {
    if (hasErrors || busy || refreshing) return;
    const update = Object.fromEntries(FIELDS.map((f) => [f.key, Number(values[f.key])])) as unknown as GuardrailUpdate;
    onSave(update, raised);
  }

  return (
    <div className={styles.card}>
      <p className={styles.title}>{guardrail.scope_label}</p>
      <div className={styles.grid}>
        {FIELDS.map((field) => (
          <div key={field.key} className={styles.field}>
            <label className={styles.label} htmlFor={`${idPrefix}-${field.key}`}>
              {field.label} {field.suffix ? `(${field.suffix})` : ""}
            </label>
            <input
              id={`${idPrefix}-${field.key}`}
              className={styles.input}
              type="number"
              disabled={busy || refreshing}
              value={values[field.key] ?? ""}
              aria-invalid={Boolean(errors[field.key])}
              onChange={(e) => setValues((prev) => ({ ...prev, [field.key]: e.target.value }))}
            />
            {errors[field.key] ? <span className={styles.error}>{errors[field.key]}</span> : null}
          </div>
        ))}
      </div>
      <div className={styles.footer}>
        <button type="button" className={styles.save} disabled={hasErrors || busy || refreshing} onClick={handleSave}>
          {busy ? "Guardando…" : refreshing ? "Actualizando…" : "Guardar"}
        </button>
        {raised ? <span className={styles.savedHint}>Sube un límite: pedirá confirmación.</span> : null}
      </div>
    </div>
  );
}

function validate(values: Record<string, string>): Record<string, string> {
  const errors: Record<string, string> = {};
  const num = (key: string) => Number(values[key]);

  for (const field of FIELDS) {
    if (values[field.key]?.trim() === "" || !Number.isFinite(num(field.key))) errors[field.key] = "Introduce un número";
    else if (num(field.key) < 0) errors[field.key] = "No puede ser negativo";
  }
  if (!errors.budget_floor && !errors.budget_ceiling && num("budget_floor") > num("budget_ceiling")) {
    errors.budget_floor = "El suelo no puede superar el techo";
  }
  if (!errors.max_step_pct && (num("max_step_pct") <= 0 || num("max_step_pct") > 100)) {
    errors.max_step_pct = "Rango seguro: 1–100 %";
  }
  if (!errors.max_changes_per_entity_per_day && num("max_changes_per_entity_per_day") < 1) {
    errors.max_changes_per_entity_per_day = "Al menos 1";
  }
  if (!errors.max_changes_per_entity_per_day && !Number.isInteger(num("max_changes_per_entity_per_day"))) errors.max_changes_per_entity_per_day = "Usa un número entero";
  if (!errors.daily_cap && !errors.monthly_cap && num("daily_cap") * 20 > num("monthly_cap") * 1.5) {
    errors.daily_cap = "El tope diario parece alto frente al mensual";
  }
  return errors;
}

import { useId, useState, type FormEvent } from "react";
import { useGuardrailSetup, useUpdateGuardrail } from "@/api/queries/rules";
import { guardrailUpdateSchema, type GuardrailUpdate } from "@/api/schemas/rules";
import { TypedConfirmDialog } from "@/components/common/TypedConfirmDialog";
import { describeApiError } from "@/utils/apiError";
import styles from "./SpendLimitCard.module.css";

const fields = [
  ["daily_cap", "Tope diario de cuenta", "0.01"],
  ["monthly_cap", "Tope mensual de cuenta", "0.01"],
  ["budget_floor", "Presupuesto mínimo de campaña", "0.01"],
  ["budget_ceiling", "Presupuesto máximo de campaña", "0.01"],
  ["max_step_pct", "Cambio máximo de presupuesto (%)", "1"],
  ["max_changes_per_entity_per_day", "Cambios máximos por entidad y día", "1"],
] as const;

export function InitialGuardrails({ businessId }: { businessId: string }) {
  const query = useGuardrailSetup(businessId);
  if (query.isLoading) return <p role="status">Comprobando configuración de límites…</p>;
  if (query.isError) return <p role="alert">{describeApiError(query.error)} <button onClick={() => void query.refetch()}>Reintentar configuración</button></p>;
  return <>{query.data?.items.filter(item => item.guardrail === null).map(account => <InitialGuardrailForm key={account.account_ref} businessId={businessId} account={account} />)}</>;
}

export function InitialGuardrailForm({ businessId, account }: { businessId: string; account: { account_ref: string; platform: string; platform_account_id: string; currency: string } }) {
  const id = useId();
  const mutation = useUpdateGuardrail(businessId);
  const [values, setValues] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<GuardrailUpdate | null>(null);
  const [error, setError] = useState("");
  function review(event: FormEvent) {
    event.preventDefault();
    const parsed = guardrailUpdateSchema.safeParse({ ...Object.fromEntries(fields.map(([key]) => [key, values[key]?.trim() ? Number(values[key]) : NaN])), min_viable_spend: 0 });
    if (!parsed.success || parsed.data.budget_floor > parsed.data.budget_ceiling || parsed.data.daily_cap > parsed.data.monthly_cap) {
      setError("Completa todos los límites. El mínimo no puede superar al máximo ni el tope diario al mensual."); return;
    }
    setError(""); setPending(parsed.data);
  }
  return <section className={styles.card}>
    <h3 className={styles.title}>Configurar límites · {account.platform} · {account.platform_account_id}</h3>
    <p>Esta cuenta aún no tiene una política completa. Confirma sus límites en {account.currency}; no son el presupuesto total de un evento ni sustituyen los topes del proveedor. Guardarlos no aprueba campañas.</p>
    <form onSubmit={review}>
      <div className={styles.row}>{fields.map(([key, label, step]) => <div className={styles.field} key={key}>
        <label className={styles.label} htmlFor={`${id}-${key}`}>{label}{key.includes("cap") || key.startsWith("budget_") ? ` (${account.currency})` : ""}</label>
        <input className={styles.input} id={`${id}-${key}`} type="number" required step={step} min={key === "budget_floor" ? 0 : step} max={key === "max_step_pct" ? 100 : undefined} value={values[key] ?? ""} disabled={mutation.isPending} onChange={e => setValues(current => ({ ...current, [key]: e.target.value }))} />
      </div>)}</div>
      <button className={styles.save} disabled={mutation.isPending}>Revisar límites de esta cuenta</button>
      {error && <p role="alert" className={styles.error}>{error}</p>}
    </form>
    {pending && <TypedConfirmDialog title="Confirmar límites de seguridad" confirmWord="CONFIGURAR" confirmLabel="Guardar límites" onClose={() => setPending(null)} onConfirm={async () => { await mutation.mutateAsync({ guardrailId: account.account_ref, update: pending }); setPending(null); }}
      description={`${account.platform} · ${account.platform_account_id}: ${pending.daily_cap} ${account.currency}/día y ${pending.monthly_cap} ${account.currency}/mes. Presupuesto de campaña: ${pending.budget_floor}–${pending.budget_ceiling} ${account.currency}. Cambio máximo: ${pending.max_step_pct} %, hasta ${pending.max_changes_per_entity_per_day} cambios por entidad y día. No aprueba ni activa campañas.`}
    />}
  </section>;
}

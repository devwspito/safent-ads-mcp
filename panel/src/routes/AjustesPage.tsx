import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMe } from "@/api/queries/auth";
import { useGuardrails, useUpdateGuardrail } from "@/api/queries/rules";
import { useSettings, useUpdateSettings } from "@/api/queries/settings";
import type { GuardrailUpdate } from "@/api/schemas/rules";
import type { SettingsUpdate } from "@/api/schemas/settings";
import { SpendLimitCard } from "@/components/settings/SpendLimitCard";
import { TopesSection } from "@/components/settings/TopesSection";
import { TelegramPairingCard } from "@/components/connections/TelegramPairingCard";
import { PageHeader } from "@/components/layout/PageHeader";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { TypedConfirmDialog } from "@/components/common/TypedConfirmDialog";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { describeApiError } from "@/utils/apiError";
import { ConexionesPage } from "./ConexionesPage";
import styles from "./AjustesPage.module.css";

/**
 * «Ajustes» — design.md §6: Tus cuentas, Límites de gasto, Avisos, Preferencias, y dos enlaces
 * discretos al pie (Tu negocio, Opciones avanzadas). Fusiona lo que antes eran dos pantallas
 * (Conexiones + Ajustes); Marca, Ofertas y economía, Calendario de eventos y Conversiones no son
 * tarea diaria del dueño y viven en `/ajustes/negocio` (design.md §6.1.3/§6.4).
 */
export function AjustesPage() {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  return <BusinessAjustesPage key={businessId} businessId={businessId} />;
}

function BusinessAjustesPage({ businessId }: { businessId: string }) {
  const settingsQuery = useSettings(businessId);
  const updateSettings = useUpdateSettings(businessId);
  const guardrailsQuery = useGuardrails(businessId);
  const updateGuardrail = useUpdateGuardrail(businessId);

  const [form, setForm] = useState<SettingsUpdate | null>(null);
  const [saved, setSaved] = useState(false);
  const [pendingGuardrail, setPendingGuardrail] = useState<{ guardrailId: string; update: GuardrailUpdate } | null>(null);
  const [savedGuardrailId, setSavedGuardrailId] = useState<string | null>(null);

  useEffect(() => {
    if (settingsQuery.data && !form) {
      const { active_hours, digest_hour, theme } = settingsQuery.data;
      setForm({ active_hours, digest_hour, theme });
    }
  }, [settingsQuery.data, form]);

  function update<K extends keyof SettingsUpdate>(key: K, value: SettingsUpdate[K]) {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
    setSaved(false);
  }

  async function handleSave() {
    if (!form) return;
    await updateSettings.mutateAsync(form);
    setSaved(true);
  }

  async function saveGuardrail(guardrailId: string, update: GuardrailUpdate) {
    setSavedGuardrailId(null);
    await updateGuardrail.mutateAsync({ guardrailId, update });
    setSavedGuardrailId(guardrailId);
  }

  return (
    <div>
      <PageHeader title="Ajustes" />

      <ConexionesPage />

      <section className={styles.section}>
        <h2 className={styles.groupTitle}>Límites de gasto</h2>
        <QueryBoundary
          isLoading={guardrailsQuery.isLoading}
          isError={guardrailsQuery.isError}
          error={guardrailsQuery.error}
          onRetry={() => void guardrailsQuery.refetch()}
          data={guardrailsQuery.data}
          isEmpty={(data) => data.items.length === 0}
          emptyTitle="Sin límites configurados"
          emptyBody="Conecta una cuenta para fijar cuánto puede gastar al día y al mes."
        >
          {(data) => (
            <>
              {updateGuardrail.isError ? <p role="alert" className={styles.deleteError}>{describeApiError(updateGuardrail.error)}</p> : null}
              {data.items.map((guardrail) => (
                <SpendLimitCard
                  key={guardrail.guardrail_id}
                  guardrail={guardrail}
                  busy={updateGuardrail.isPending}
                  justSaved={savedGuardrailId === guardrail.guardrail_id}
                  onSave={(nextUpdate, requiresConfirm) => {
                    if (requiresConfirm) setPendingGuardrail({ guardrailId: guardrail.guardrail_id, update: nextUpdate });
                    else void saveGuardrail(guardrail.guardrail_id, nextUpdate);
                  }}
                />
              ))}
            </>
          )}
        </QueryBoundary>
      </section>

      <TopesSection businessId={businessId} />

      <QueryBoundary
        isLoading={settingsQuery.isLoading}
        isError={settingsQuery.isError}
        error={settingsQuery.error}
        onRetry={() => void settingsQuery.refetch()}
        data={settingsQuery.data}
        isEmpty={() => false}
        emptyTitle=""
      >
        {(data) =>
          form ? (
            <>
              <section className={styles.section}>
                <h2 className={styles.groupTitle}>Avisos</h2>
                <div className={styles.form}>
                  <div className={styles.field}>
                    <label className={styles.label} htmlFor="digest-hour">
                      Hora del resumen diario
                    </label>
                    <input
                      id="digest-hour"
                      className={styles.input}
                      type="time"
                      value={form.digest_hour}
                      onChange={(e) => update("digest_hour", e.target.value)}
                    />
                  </div>

                  <div className={styles.row}>
                    <div className={styles.field}>
                      <label className={styles.label} htmlFor="active-start">
                        Horario activo — desde
                      </label>
                      <input
                        id="active-start"
                        className={styles.input}
                        type="time"
                        value={form.active_hours.start}
                        onChange={(e) => update("active_hours", { ...form.active_hours, start: e.target.value })}
                      />
                    </div>
                    <div className={styles.field}>
                      <label className={styles.label} htmlFor="active-end">
                        Horario activo — hasta
                      </label>
                      <input
                        id="active-end"
                        className={styles.input}
                        type="time"
                        value={form.active_hours.end}
                        onChange={(e) => update("active_hours", { ...form.active_hours, end: e.target.value })}
                      />
                    </div>
                  </div>

                  <TelegramPairingCard businessId={businessId} />
                </div>
              </section>

              <section className={styles.section}>
                <h2 className={styles.groupTitle}>Preferencias</h2>
                <div className={styles.form}>
                  <div className={styles.field}>
                    <label className={styles.label} htmlFor="theme">
                      Tema
                    </label>
                    <select
                      id="theme"
                      className={styles.select}
                      value={form.theme}
                      onChange={(e) => update("theme", e.target.value as SettingsUpdate["theme"])}
                    >
                      <option value="system">Según el sistema</option>
                      <option value="light">Claro</option>
                      <option value="dark">Oscuro</option>
                    </select>
                  </div>

                  <div className={styles.row}>
                    <div className={styles.field}>
                      <span className={styles.label}>Zona horaria</span>
                      <span className={styles.readonlyValue}>{data.timezone}</span>
                    </div>
                    <div className={styles.field}>
                      <span className={styles.label}>Moneda</span>
                      <span className={styles.readonlyValue}>{data.currency}</span>
                    </div>
                  </div>

                  <button
                    type="button"
                    className={styles.save}
                    aria-label={updateSettings.isPending ? undefined : "Guardar preferencias y avisos"}
                    onClick={() => void handleSave()}
                    disabled={updateSettings.isPending}
                  >
                    {updateSettings.isPending ? "Guardando…" : "Guardar"}
                  </button>
                  {saved ? <span className={styles.savedHint}>Guardado.</span> : null}
                </div>
              </section>
            </>
          ) : null
        }
      </QueryBoundary>

      <div className={styles.footerLinks}>
        <Link className={styles.advancedLink} to="/ajustes/negocio">
          Tu negocio →
        </Link>
        <Link className={styles.advancedLink} to="/ajustes/avanzado">
          Opciones avanzadas →
        </Link>
      </div>

      {pendingGuardrail ? (
        <TypedConfirmDialog
          title="Subir un límite de gasto"
          description="Vas a subir un tope. Escribe SUBIR para confirmar."
          confirmLabel="Subir"
          confirmWord="SUBIR"
          danger
          onConfirm={async () => {
            await saveGuardrail(pendingGuardrail.guardrailId, pendingGuardrail.update);
            setPendingGuardrail(null);
          }}
          onClose={() => setPendingGuardrail(null)}
        />
      ) : null}
    </div>
  );
}

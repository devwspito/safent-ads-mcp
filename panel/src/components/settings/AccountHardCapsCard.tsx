import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type MouseEvent } from "react";
import { useMe } from "@/api/queries/auth";
import { useAccountHardCaps, useDeleteAccountHardCaps, useSetAccountHardCaps } from "@/api/queries/hardCaps";
import type { PlatformAccount } from "@/api/schemas/connections";
import type { CappedField, EffectiveCaps, HardCapsView } from "@/api/schemas/hardCaps";
import { ActionConfirmationDialog } from "@/components/common/ActionConfirmationDialog";
import { FreshIdentificationPrompt } from "@/components/common/FreshIdentificationPrompt";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { Skeleton } from "@/components/states/Skeleton";
import { useHardCapsFlow, type HardCapsAction, type HardCapsCredentials } from "@/hooks/useHardCapsFlow";
import type { ReauthMethod } from "@/utils/apiError";
import {
  describeAccountsLeft,
  describeChangesLeftToday,
  describeClampedField,
  describeHardCapsError,
  describeSource,
  formatMinor,
  formatMinorForInput,
  raisesTheEffectiveCap,
  validateCapsForm,
  type CapsFormField,
  type CapsFormValues,
} from "@/utils/hardCaps";
import styles from "./TopesSection.module.css";

const EMPTY_FORM: CapsFormValues = { daily: "", monthly: "", ceiling: "" };

// El MISMO vocabulario en la lista de importes, en el formulario y en el resumen de la
// confirmación: tres nombres para el mismo tope obligan a traducir mentalmente justo cuando se
// está a punto de confirmar un gasto.
const FIELD_LABELS: Record<CapsFormField, string> = {
  daily: "Al día",
  monthly: "Al mes",
  ceiling: "Techo",
};

/** Lo que está en vigor, no lo que se guardó: el campo arranca en el efectivo que aplica el bróker. */
function formValuesFrom(view: HardCapsView): CapsFormValues {
  const effective = view.effective;
  if (effective === null) return EMPTY_FORM;
  return {
    daily: formatMinorForInput(effective.daily_cap_minor),
    monthly: formatMinorForInput(effective.monthly_cap_minor),
    ceiling: formatMinorForInput(effective.ceiling_minor),
  };
}

function presenceDescription(methods: ReauthMethod[]): string {
  if (methods.includes("federated") && !methods.includes("totp")) {
    return "Vuelve a identificarte con Google para continuar.";
  }
  return "Escribe el código de tu aplicación de autenticación para continuar.";
}

/**
 * Una cuenta y su tope (spec 008 §5, `contracts/hard-caps.openapi.yaml`): el efectivo que el
 * bróker aplicará, de dónde viene, qué quedó recortado y qué margen deja el sobre.
 *
 * Tres cosas que esta tarjeta NO hace, porque no le corresponden: no decide si un cambio sube
 * (lo decide `ads-api` y lo exige antes de tocar nada), no convierte divisas (la divisa sale del
 * sobre y viaja como confirmación) y no relaja ningún tope — sin sobre declarado no hay
 * formulario, porque todo PUT sería un 409.
 */
export function AccountHardCapsCard({ account }: { account: PlatformAccount }) {
  const accountId = account.external_account_id;
  const capsQuery = useAccountHardCaps(accountId);
  const meQuery = useMe();
  const setCaps = useSetAccountHardCaps(accountId);
  const deleteCaps = useDeleteAccountHardCaps(accountId);
  const [values, setValues] = useState<CapsFormValues | null>(null);
  const [showErrors, setShowErrors] = useState(false);
  const [justSaved, setJustSaved] = useState(false);
  const fieldIds = useId();
  const saveButtonRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const inputRefs = useRef<Record<CapsFormField, HTMLInputElement | null>>({
    daily: null,
    monthly: null,
    ceiling: null,
  });

  const view = capsQuery.data;

  useEffect(() => {
    if (view && values === null) setValues(formValuesFrom(view));
  }, [view, values]);

  const submit = useCallback(
    (action: HardCapsAction, credentials: HardCapsCredentials) =>
      action.kind === "set"
        ? setCaps.mutateAsync({ caps: action.caps, ...credentials })
        : deleteCaps.mutateAsync(credentials),
    [setCaps, deleteCaps],
  );

  const flow = useHardCapsFlow({
    submit,
    federatedLoginAvailable: meQuery.data?.federated_login_available ?? false,
    onApplied: (applied) => {
      setValues(formValuesFrom(applied));
      setShowErrors(false);
      setJustSaved(true);
    },
  });

  const envelope = view?.envelope ?? null;
  const validation = values !== null && envelope !== null ? validateCapsForm(values, envelope) : null;
  const isBusy = flow.presence.isSubmitting || flow.isConfirmationOpen || flow.confirmSubmitting;
  const pendingCaps = flow.pendingAction?.kind === "set" ? flow.pendingAction.caps : null;

  function update(field: CapsFormField, value: string) {
    setValues((previous) => ({ ...(previous ?? EMPTY_FORM), [field]: value }));
    setJustSaved(false);
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!validation || isBusy) return;
    setShowErrors(true);
    setJustSaved(false);
    if (validation.caps === null) {
      const firstInvalid = (Object.keys(FIELD_LABELS) as CapsFormField[]).find((field) => validation.errors[field]);
      if (firstInvalid) inputRefs.current[firstInvalid]?.focus();
      return;
    }
    triggerRef.current = saveButtonRef.current;
    flow.start({ kind: "set", caps: validation.caps });
  }

  function handleWithdraw(event: MouseEvent<HTMLButtonElement>) {
    triggerRef.current = event.currentTarget;
    setJustSaved(false);
    flow.start({ kind: "withdraw" });
  }

  // Un 5xx (o una identificación fresca perdida) puede cerrar un diálogo sin que su mensaje se
  // vea dentro de él — sin esto, el clic "no hace nada".
  const outsideDialogError =
    flow.submitError ??
    (flow.presence.errorMessage && !flow.presence.isPromptOpen ? flow.presence.errorMessage : null) ??
    (flow.confirmError && !flow.isConfirmationOpen ? flow.confirmError : null);

  return (
    <article className={styles.card}>
      <h3 className={styles.cardTitle}>{account.label}</h3>
      <p className={styles.meta}>
        <code>{accountId}</code>
      </p>

      <QueryBoundary
        isLoading={capsQuery.isLoading}
        isError={capsQuery.isError}
        errorMessage={capsQuery.isError ? describeHardCapsError(capsQuery.error) : undefined}
        onRetry={() => void capsQuery.refetch()}
        data={capsQuery.data}
        isEmpty={() => false}
        emptyTitle=""
        skeleton={<Skeleton height="120px" />}
      >
        {(loaded) => (
          <>
            <p className={styles.source}>{describeSource(loaded.source)}</p>

            {loaded.effective ? (
              <>
                <dl className={styles.amounts}>
                  <Amount label="Al día" view={loaded} effective={loaded.effective} field="daily_cap_minor" />
                  <Amount label="Al mes" view={loaded} effective={loaded.effective} field="monthly_cap_minor" />
                  <Amount label="Techo" view={loaded} effective={loaded.effective} field="ceiling_minor" />
                  <div className={styles.amount}>
                    <dt className={styles.amountLabel}>Suelo</dt>
                    <dd className={styles.amountValue}>{formatMinor(loaded.effective.floor_minor, loaded.currency)}</dd>
                  </div>
                </dl>
                <p className={styles.meta}>El suelo no se fija desde el panel.</p>
              </>
            ) : null}

            {loaded.panel_state_available ? null : (
              <p className={styles.notice}>Estado del panel no disponible: se aplica solo el fichero.</p>
            )}

            {loaded.envelope === null ? (
              <p className={styles.notice}>
                El sobre de gasto no está declarado: decláralo como panel_managed en config/caps.yaml para fijar
                topes desde aquí.
              </p>
            ) : (
              <form className={styles.form} onSubmit={handleSubmit} noValidate>
                <div className={styles.row}>
                  {(Object.keys(FIELD_LABELS) as CapsFormField[]).map((field) => {
                    const error = showErrors ? validation?.errors[field] : undefined;
                    const inputId = `${fieldIds}-${field}`;
                    return (
                      <div className={styles.field} key={field}>
                        <label className={styles.label} htmlFor={inputId}>
                          {FIELD_LABELS[field]}
                        </label>
                        <input
                          id={inputId}
                          ref={(element) => {
                            inputRefs.current[field] = element;
                          }}
                          className={styles.input}
                          type="text"
                          inputMode="decimal"
                          autoComplete="off"
                          value={values?.[field] ?? ""}
                          disabled={isBusy}
                          aria-invalid={Boolean(error)}
                          aria-describedby={error ? `${inputId}-error` : undefined}
                          onChange={(event) => update(field, event.target.value)}
                        />
                        {error ? (
                          <span className={styles.error} id={`${inputId}-error`} role="alert">
                            {error}
                          </span>
                        ) : null}
                      </div>
                    );
                  })}
                </div>

                <p className={styles.meta}>
                  Sobre: hasta {formatMinor(loaded.envelope.max_daily_cap_minor, loaded.envelope.currency)} al día,{" "}
                  {formatMinor(loaded.envelope.max_monthly_cap_minor, loaded.envelope.currency)} al mes y{" "}
                  {formatMinor(loaded.envelope.max_ceiling_minor, loaded.envelope.currency)} de techo.
                </p>
                <p className={styles.meta}>{describeAccountsLeft(loaded.envelope)}</p>
                <p className={styles.meta}>{describeChangesLeftToday(loaded.envelope)}</p>

                <div className={styles.actions}>
                  <button type="submit" className={styles.save} ref={saveButtonRef} disabled={isBusy}>
                    {isBusy ? "Guardando…" : "Guardar topes"}
                  </button>
                  {loaded.source === "panel" || loaded.source === "file_and_panel" ? (
                    <button type="button" className={styles.withdraw} onClick={handleWithdraw} disabled={isBusy}>
                      Quitar el tope del panel
                    </button>
                  ) : null}
                  {justSaved ? (
                    <span className={styles.saved} role="status">
                      Guardado.
                    </span>
                  ) : null}
                </div>

                {validation?.caps && raisesTheEffectiveCap(loaded, validation.caps) ? (
                  <p className={styles.meta}>Subir un tope te pedirá identificarte.</p>
                ) : null}
                {loaded.source === "file_and_panel" ? (
                  <p className={styles.meta}>Quitar el tope del panel deja el del fichero, que es mayor.</p>
                ) : null}
              </form>
            )}
          </>
        )}
      </QueryBoundary>

      {outsideDialogError ? (
        <p className={styles.formError} role="alert">
          {outsideDialogError}
        </p>
      ) : null}

      {flow.presence.isPromptOpen ? (
        <FreshIdentificationPrompt
          title="Confirma que eres tú"
          description={presenceDescription(flow.presence.methods)}
          confirmLabel="Continuar"
          methods={flow.presence.methods}
          freshUntil={meQuery.data?.session?.fresh_identification_until}
          isSubmitting={flow.presence.isSubmitting}
          isStartingGoogle={flow.presence.isStartingGoogle}
          errorMessage={flow.presence.errorMessage}
          onConfirmTotp={flow.presence.confirm}
          onConfirmWithGoogle={flow.presence.confirmWithGoogle}
          onClose={flow.presence.cancel}
          returnFocusTo={triggerRef}
        />
      ) : null}

      {flow.isConfirmationOpen ? (
        <ActionConfirmationDialog
          title="Revisa y confirma esta acción"
          description={
            pendingCaps
              ? `Vas a fijar el tope de ${account.label}.`
              : `Vas a quitar el tope del panel de ${account.label}.`
          }
          confirmLabel={pendingCaps ? "Guardar topes" : "Quitar el tope"}
          summary={
            pendingCaps
              ? [
                  `Al día: ${formatMinor(pendingCaps.daily_cap_minor, pendingCaps.currency)}`,
                  `Al mes: ${formatMinor(pendingCaps.monthly_cap_minor, pendingCaps.currency)}`,
                  `Techo: ${formatMinor(pendingCaps.ceiling_minor, pendingCaps.currency)}`,
                ]
              : ["Se quita el tope fijado desde el panel."]
          }
          canConfirm={!flow.confirmSubmitting}
          isSubmitting={flow.confirmSubmitting}
          errorMessage={flow.confirmError}
          onConfirm={flow.confirm}
          onClose={flow.cancelConfirmation}
          returnFocusTo={triggerRef}
        />
      ) : null}
    </article>
  );
}

/** Un importe en vigor y, si quedó recortado, en qué quedó — nunca uno sin el otro. */
function Amount({
  label,
  view,
  effective,
  field,
}: {
  label: string;
  view: HardCapsView;
  effective: EffectiveCaps;
  field: CappedField;
}) {
  const clamped = describeClampedField(view, field);
  return (
    <div className={styles.amount}>
      <dt className={styles.amountLabel}>{label}</dt>
      <dd className={styles.amountValue}>
        {formatMinor(effective[field], view.currency)}
        {clamped ? <span className={styles.clamped}>{clamped}</span> : null}
      </dd>
    </div>
  );
}

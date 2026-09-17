import { useId, useState } from "react";
import { useOfferings, useUpdateOfferingEconomics } from "@/api/queries/economics";
import type { Offering, OfferingEconomicsInput, PaymentPlan } from "@/api/schemas/economics";
import { ApiRequestError } from "@/api/client";
import { ErrorState } from "@/components/states/ErrorState";
import { Skeleton } from "@/components/states/Skeleton";
import { formatMoney } from "@/utils/money";
import styles from "./OfferingsEconomicsTable.module.css";
import { CreateOfferingForm } from "./CreateOfferingForm";

interface OfferingsEconomicsTableProps {
  businessId: string;
}

const PAYMENT_PLAN_LABELS: Record<PaymentPlan, string> = {
  none: "Cobro único",
  instalments: "A plazos",
};

const DEFAULT_ERROR_MESSAGE = "No se pudo guardar la economía de esta oferta.";

interface RowForm {
  vatRatePct: string;
  deliveryCostEur: string;
  salesCostEur: string;
  refundRatePct: string;
  paymentPlan: PaymentPlan;
}

function formFromOffering(offering: Offering): RowForm {
  const economics = offering.economics;
  return {
    vatRatePct: economics ? String(economics.vat_rate_pct) : "",
    deliveryCostEur: economics ? String(economics.delivery_cost_minor / 100) : "",
    salesCostEur: economics ? String(economics.sales_cost_minor / 100) : "",
    refundRatePct: economics?.refund_rate_pct !== null && economics?.refund_rate_pct !== undefined ? String(economics.refund_rate_pct) : "",
    paymentPlan: economics?.payment_plan ?? "none",
  };
}

function toEconomicsInput(form: RowForm, currency: string): OfferingEconomicsInput | null {
  const vatRatePct = Number(form.vatRatePct);
  const deliveryCostEur = Number(form.deliveryCostEur);
  const salesCostEur = Number(form.salesCostEur);
  if (form.vatRatePct === "" || form.deliveryCostEur === "" || form.salesCostEur === "") return null;
  if (Number.isNaN(vatRatePct) || Number.isNaN(deliveryCostEur) || Number.isNaN(salesCostEur)) return null;
  return {
    vat_rate_pct: vatRatePct,
    delivery_cost_minor: Math.round(deliveryCostEur * 100),
    sales_cost_minor: Math.round(salesCostEur * 100),
    refund_rate_pct: form.refundRatePct === "" ? null : Number(form.refundRatePct),
    payment_plan: form.paymentPlan,
    currency,
  };
}

function OfferingRow({ businessId, offering }: { businessId: string; offering: Offering }) {
  const [form, setForm] = useState<RowForm>(() => formFromOffering(offering));
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const updateEconomics = useUpdateOfferingEconomics(businessId);
  const rowId = useId();
  const currency = offering.economics?.currency ?? offering.list_price?.currency ?? "EUR";
  const isProvisional = offering.economics === null;

  function update<K extends keyof RowForm>(key: K, value: RowForm[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
  }

  async function handleSave() {
    setErrorMessage(null);
    setSaved(false);
    const input = toEconomicsInput(form, currency);
    if (!input) {
      setErrorMessage("IVA, coste de entrega y coste comercial son obligatorios.");
      return;
    }
    try {
      await updateEconomics.mutateAsync({ offeringId: offering.offering_id, economics: input });
      setSaved(true);
    } catch (error) {
      setErrorMessage(error instanceof ApiRequestError ? error.message : DEFAULT_ERROR_MESSAGE);
    }
  }

  return (
    <li className={styles.row}>
      <div className={styles.rowHeader}>
        <span className={styles.title}>{offering.title}</span>
        {offering.list_price ? <span className={styles.price}>{formatMoney(offering.list_price)}</span> : null}
        {isProvisional ? (
          <span className={styles.badge} title="Provisional: introduce IVA, coste de entrega y coste comercial para calcular el margen real.">
            Provisional
          </span>
        ) : null}
      </div>

      <div className={styles.fields}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${rowId}-vat`}>
            IVA (%)
          </label>
          <input
            id={`${rowId}-vat`}
            className={styles.input}
            type="number"
            min={0}
            max={100}
            step="0.01"
            value={form.vatRatePct}
            onChange={(e) => update("vatRatePct", e.target.value)}
          />
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${rowId}-delivery`}>
            Coste de entrega (€)
          </label>
          <input
            id={`${rowId}-delivery`}
            className={styles.input}
            type="number"
            min={0}
            step="0.01"
            value={form.deliveryCostEur}
            onChange={(e) => update("deliveryCostEur", e.target.value)}
          />
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${rowId}-sales`}>
            Coste comercial (€/mes)
          </label>
          <input
            id={`${rowId}-sales`}
            className={styles.input}
            type="number"
            min={0}
            step="0.01"
            value={form.salesCostEur}
            onChange={(e) => update("salesCostEur", e.target.value)}
          />
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${rowId}-refund`}>
            Devolución (%, opcional)
          </label>
          <input
            id={`${rowId}-refund`}
            className={styles.input}
            type="number"
            min={0}
            max={100}
            step="0.01"
            value={form.refundRatePct}
            onChange={(e) => update("refundRatePct", e.target.value)}
          />
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${rowId}-plan`}>
            Plan de pago
          </label>
          <select
            id={`${rowId}-plan`}
            className={styles.select}
            value={form.paymentPlan}
            onChange={(e) => update("paymentPlan", e.target.value as PaymentPlan)}
          >
            {(Object.keys(PAYMENT_PLAN_LABELS) as PaymentPlan[]).map((plan) => (
              <option key={plan} value={plan}>
                {PAYMENT_PLAN_LABELS[plan]}
              </option>
            ))}
          </select>
        </div>

        <button
          type="button"
          className={styles.save}
          onClick={() => void handleSave()}
          disabled={updateEconomics.isPending}
          aria-label={`Guardar economía de ${offering.title}`}
        >
          {updateEconomics.isPending ? "Guardando…" : "Guardar"}
        </button>
      </div>

      {saved ? (
        <span className={styles.status} role="status">
          Guardado.
        </span>
      ) : null}
      {errorMessage ? (
        <span className={styles.error} role="alert">
          {errorMessage}
        </span>
      ) : null}
    </li>
  );
}

/** Tabla de ofertas con su economía unitaria (T131/T132, contracts/rest-api.md §Economía unitaria):
 * IVA, coste de entrega, coste comercial mensual, devolución y plan de pago por oferta, con edición
 * en línea. `Provisional` cuando el dueño todavía no ha rellenado esos números — el motor sigue
 * `profitability-engine.md §1: 'CM = price × 0,60'` hasta entonces, y no propone subidas de gasto. */
export function OfferingsEconomicsTable({ businessId }: OfferingsEconomicsTableProps) {
  const offeringsQuery = useOfferings(businessId);

  if (offeringsQuery.isLoading) return <Skeleton height="120px" />;
  if (offeringsQuery.isError) {
    return <ErrorState message="No se pudieron cargar las ofertas." onRetry={() => void offeringsQuery.refetch()} />;
  }

  const offerings = offeringsQuery.data?.items ?? [];
  return (
    <><CreateOfferingForm key={businessId} businessId={businessId} />{offerings.length === 0
      ? <p className={styles.empty}>Todavía no hay ofertas para este negocio.</p>
      : <ul className={styles.list}>
      {offerings.map((offering) => (
        <OfferingRow key={offering.offering_id} businessId={businessId} offering={offering} />
      ))}
    </ul>}</>
  );
}

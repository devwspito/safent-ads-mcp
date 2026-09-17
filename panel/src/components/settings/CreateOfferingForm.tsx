import { useId, useState, type FormEvent } from "react";
import { useCreateOffering } from "@/api/queries/economics";
import { describeApiError } from "@/utils/apiError";
import { ApiRequestError } from "@/api/client";
import styles from "./OfferingsEconomicsTable.module.css";

export function CreateOfferingForm({ businessId }: { businessId: string }) {
  const id = useId();
  const create = useCreateOffering(businessId);
  const [code, setCode] = useState("");
  const [title, setTitle] = useState("");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (create.isPending) return;
    setError(null); setSaved(false);
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(code) || !title.trim() || title.trim().length > 200) {
      setError("Indica un código (letras, números, punto, guion o guion bajo) y un nombre."); return;
    }
    if ((amount !== "" || currency !== "") && (!/^[0-9]{1,10}(\.[0-9]{1,2})?$/.test(amount) || !/^[A-Z]{3}$/.test(currency))) {
      setError("Indica precio y moneda juntos, o deja ambos vacíos. Usa hasta dos decimales con punto."); return;
    }
    try {
      await create.mutateAsync({ code, title: title.trim(), price_amount: amount || null, price_currency: currency || null });
      setCode(""); setTitle(""); setAmount(""); setCurrency(""); setSaved(true);
    } catch (err) {
      setError(err instanceof ApiRequestError && err.code === "OFFERING_CODE_CONFLICT"
        ? "Ese código ya identifica otra oferta. Revisa el catálogo o elige otro código."
        : describeApiError(err));
    }
  }

  return <form onSubmit={event => void submit(event)} aria-label="Crear oferta">
    <h4>Nueva oferta</h4>
    <p>Registra qué vendes para que el chat pueda preparar una propuesta. No crea ni publica anuncios.</p>
    <fieldset className={styles.fields} disabled={create.isPending}>
      <div className={styles.field}><label htmlFor={`${id}-code`}>Código de oferta</label><input className={styles.input} id={`${id}-code`} value={code} maxLength={64} required onChange={e => { setCode(e.target.value); setSaved(false); }} /></div>
      <div className={styles.field}><label htmlFor={`${id}-title`}>Nombre de oferta</label><input className={styles.input} id={`${id}-title`} value={title} maxLength={200} required onChange={e => { setTitle(e.target.value); setSaved(false); }} /></div>
      <div className={styles.field}><label htmlFor={`${id}-amount`}>Precio (opcional)</label><input className={styles.input} id={`${id}-amount`} inputMode="decimal" value={amount} onChange={e => setAmount(e.target.value)} /></div>
      <div className={styles.field}><label htmlFor={`${id}-currency`}>Moneda del precio</label><input className={styles.input} id={`${id}-currency`} placeholder="EUR, USD…" maxLength={3} value={currency} onChange={e => setCurrency(e.target.value)} /></div>
      <button className={styles.save} type="submit">{create.isPending ? "Guardando oferta…" : "Crear oferta"}</button>
    </fieldset>
    {error ? <p className={styles.error} role="alert">{error} Revisa la lista antes de reintentar; el mismo código no se duplica.</p> : null}
    {saved ? <p className={styles.status} role="status">Oferta guardada. Ya puedes usarla para preparar una propuesta en el chat.</p> : null}
  </form>;
}

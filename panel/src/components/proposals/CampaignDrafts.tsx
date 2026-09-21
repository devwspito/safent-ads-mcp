import { useState, type FormEvent } from "react";
import { useCampaignDrafts, useSaveCampaignDraft, usePromoteCampaignDraft, type CampaignDraft } from "@/api/queries/campaignDrafts";
import { describeApiError } from "@/utils/apiError";
import styles from "./CampaignDrafts.module.css";

const labels: Record<string, string> = { title: "nombre", platform: "plataforma", offering_id: "oferta", account_ref: "cuenta publicitaria", objective: "objetivo", daily_budget: "presupuesto diario y moneda", duration_days: "duración", success_criterion: "criterio de éxito", kill_criterion: "criterio de parada", angle: "enfoque", targeting_seed: "audiencia", landing_url: "URL de reserva o destino", creation_plan: "plan nativo pausado" };

export function CampaignDrafts({ businessId, draftId, hidePromotion = false }: { businessId: string; draftId?: string; hidePromotion?: boolean }) {
  const query = useCampaignDrafts(businessId);
  const save = useSaveCampaignDraft(businessId);
  const [title, setTitle] = useState("");
  const [key, setKey] = useState(() => `draft-${crypto.randomUUID()}`);
  const [error, setError] = useState<string | null>(null);
  async function create(event: FormEvent) {
    event.preventDefault();
    if (save.isPending || !title.trim()) return;
    setError(null);
    try { await save.mutateAsync({ draft_key: key, changes: { title: title.trim() } }); setTitle(""); setKey(`draft-${crypto.randomUUID()}`); }
    catch (err) { setError(describeApiError(err)); }
  }
  return <section aria-label="Borradores de campañas" className={styles.section}>
    <h2>Borradores — no ejecutables</h2>
    <p>Guarda la idea ahora y completa presupuesto, destino y detalles más tarde desde el chat o aquí. Guardar no crea campañas ni concede aprobación.</p>
    {!draftId ? <form onSubmit={event => void create(event)} className={styles.form}>
      <label>Nombre del borrador<input value={title} maxLength={128} required onChange={event => setTitle(event.target.value)} /></label>
      <button disabled={save.isPending || !businessId} type="submit">Guardar borrador</button>
    </form> : null}
    {error ? <p role="alert">{error} Revisa los borradores antes de reintentar.</p> : null}
    {query.isError ? <p role="alert">No se han podido cargar los borradores. <button onClick={() => void query.refetch()}>Reintentar lectura</button></p> : null}
    {query.isPending ? <p>Cargando borradores…</p> : null}
    {query.data?.items.filter(draft => !draftId || draft.draft_id === draftId).map(draft => <DraftCard key={`${draft.draft_id}:${draft.revision}`} draft={draft} businessId={businessId} hidePromotion={hidePromotion} />)}
    {query.data?.has_more ? <p>Hay más borradores; usa el chat con el ID del borrador para abrirlo.</p> : null}
  </section>;
}

function DraftCard({ draft, businessId, hidePromotion }: { draft: CampaignDraft; businessId: string; hidePromotion: boolean }) {
  const save = useSaveCampaignDraft(businessId);
  const promote = usePromoteCampaignDraft(businessId);
  const [amount, setAmount] = useState(draft.brief.daily_budget?.amount ?? "");
  const [currency, setCurrency] = useState(draft.brief.daily_budget?.currency ?? "");
  const [url, setUrl] = useState(draft.brief.landing_url ?? "");
  const [notes, setNotes] = useState(draft.brief.notes ?? "");
  const [error, setError] = useState<string | null>(null);
  const busy = save.isPending || promote.isPending;
  async function update(event: FormEvent) {
    event.preventDefault(); if (busy) return; setError(null);
    if ((amount || currency) && (!/^[0-9]{1,10}(\.[0-9]{1,2})?$/.test(amount) || currency !== "EUR")) { setError("Indica importe y moneda juntos, o deja ambos pendientes."); return; }
    try { await save.mutateAsync({ draft_key: draft.draft_key, expected_revision: draft.revision, changes: { daily_budget: amount ? { amount, currency } : null, landing_url: url || null, notes: notes || null } }); }
    catch (err) { setError(describeApiError(err)); }
  }
  async function prepare() {
    if (busy || draft.missing_fields.length) return; setError(null);
    try { await promote.mutateAsync(draft); }
    catch (err) { setError(describeApiError(err)); }
  }
  return <article className={styles.card} aria-label={`Borrador ${draft.brief.title}`}>
    <h3>{draft.brief.title}</h3>
    <p>{draft.state === "proposed" ? "Propuesta preparada; requiere revisión y aprobación independiente." : "Borrador guardado. No puede ejecutarse."}</p>
    <p>Presupuesto: {draft.brief.daily_budget ? `${draft.brief.daily_budget.amount} ${draft.brief.daily_budget.currency}/día (sin aprobar)` : "pendiente"}. Destino: {draft.brief.landing_url ?? "pendiente"}.</p>
    {draft.state === "draft" ? <>
      <p>Falta completar: {draft.missing_fields.length ? draft.missing_fields.map(field => labels[field] ?? field).join(", ") : "sin campos pendientes; falta revisión de la propuesta"}.</p>
      <details><summary>Editar presupuesto, destino y notas</summary><form onSubmit={event => void update(event)} className={styles.form}>
        <fieldset disabled={busy}>
          <label>Presupuesto diario<input value={amount} inputMode="decimal" onChange={event => setAmount(event.target.value)} /></label>
          <label>Moneda<select value={currency} onChange={event => setCurrency(event.target.value)}><option value="">Pendiente</option><option value="EUR">EUR</option></select></label>
          <label>URL de reserva o destino<input value={url} type="url" onChange={event => setUrl(event.target.value)} /></label>
          <label>Notas del borrador<textarea value={notes} maxLength={4000} onChange={event => setNotes(event.target.value)} /></label>
          <button type="submit">Guardar cambios del borrador</button>
        </fieldset>
      </form></details>
      {!hidePromotion && <button disabled={busy || draft.missing_fields.length > 0} onClick={() => void prepare()}>Preparar propuesta para revisión</button>}
    </> : <p>Propuesta: {draft.proposal_id}</p>}
    {error ? <p role="alert">{error} No se ha aprobado ni ejecutado ninguna campaña.</p> : null}
    <small>ID: {draft.draft_id} · revisión {draft.revision}. El chat puede continuar este borrador.</small>
  </article>;
}

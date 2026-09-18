import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { apiClient } from "@/api/client";
import { getAdsBasePath } from "@/utils/basePath";
import styles from "./LaunchPlans.module.css";

const slotSchema = z.object({ id: z.string(), title: z.string(), format: z.string(), copy: z.string(), uploaded: z.boolean() });
const reviewSchema = z.object({ approved: z.boolean(), approved_at: z.string().nullable() });
const planSchema = z.object({ slug: z.string(), title: z.string(), summary: z.string(), proposal_id: z.string().nullable(), landing_url: z.string(), blockers: z.array(z.string()), documents: z.array(z.object({ title: z.string(), text: z.string() })), video_slots: z.array(slotSchema), revision: z.string(), review: reviewSchema });
const schema = z.object({ items: z.array(planSchema) });
type Plan = z.infer<typeof planSchema>;

export function LaunchPlans({ businessId }: { businessId: string }) {
  const query = useQuery({ queryKey: ["launch-plans", businessId], enabled: Boolean(businessId), queryFn: () => apiClient.get("/launch-plans", schema, { business_id: businessId }), retry: false });
  if (query.isError) return <p role="alert">No se pudieron cargar los planes de lanzamiento. <button onClick={() => void query.refetch()}>Reintentar</button></p>;
  if (!query.data?.items.length) return null;
  return <section aria-label="Planes de lanzamiento" className={styles.section}>{query.data.items.map(plan => <LaunchPlan key={plan.slug} plan={plan} businessId={businessId} />)}</section>;
}

function LaunchPlan({ plan, businessId }: { plan: Plan; businessId: string }) {
  const [selected, setSelected] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const client = useQueryClient();
  const base = getAdsBasePath();
  async function approve() {
    if (busy) return;
    setBusy("review"); setMessage(null);
    try {
      await apiClient.post(`/launch-plans/${encodeURIComponent(plan.slug)}/review`, reviewSchema, { revision: plan.revision }, { business_id: businessId });
      setMessage("Plan revisado y aprobado. No se han activado campañas, gasto ni envíos.");
      await client.invalidateQueries({ queryKey: ["launch-plans", businessId] });
    } catch { setMessage("No se pudo guardar la revisión. Recarga el plan antes de reintentar."); }
    finally { setBusy(null); }
  }
  async function upload(slot: string, file?: File) {
    if (!file || busy) return;
    if (file.size > 100 * 1024 * 1024 || !file.name.toLowerCase().endsWith(".mp4")) { setMessage("Selecciona un MP4 de hasta 100 MB."); return; }
    setBusy(slot); setMessage(null);
    try {
      const csrf = document.cookie.split("; ").find(value => value.startsWith("ads_csrf="))?.slice("ads_csrf=".length);
      const response = await fetch(`${base}/api/v1/launch-plans/${encodeURIComponent(plan.slug)}/videos/${encodeURIComponent(slot)}?business_id=${encodeURIComponent(businessId)}`, { method: "PUT", credentials: "same-origin", headers: { "Content-Type": "video/mp4", "X-CSRF-Token": decodeURIComponent(csrf ?? "") }, body: file });
      const result = await response.json() as { error?: { message?: string } };
      if (!response.ok) throw new Error(result.error?.message ?? "No se ha podido guardar el vídeo.");
      setMessage("Vídeo guardado en el panel. No se ha enviado ni publicado en Meta.");
      await client.invalidateQueries({ queryKey: ["launch-plans", businessId] });
    } catch (error) { setMessage(error instanceof Error ? error.message : "Error al cargar el vídeo."); }
    finally { setBusy(null); }
  }
  return <article className={styles.card}>
    <div className={styles.header}><div><span className={styles.eyebrow}>Lanzamiento · preparación para revisión</span><h2>{plan.title}</h2><p>{plan.summary}</p></div><a className={styles.link} href={`${base}/eventos/${encodeURIComponent(plan.slug)}`} target="_blank" rel="noreferrer">Ver landing ↗</a></div>
    {plan.proposal_id ? <p>Propuesta de campaña asociada: <code>{plan.proposal_id}</code>. Su aprobación se realiza en la tarjeta de propuesta.</p> : null}
    {plan.blockers.length ? <details className={styles.blockers} open><summary>Pendientes antes de activar ({plan.blockers.length})</summary><ul>{plan.blockers.map(item => <li key={item}>{item}</li>)}</ul></details> : null}
    <div className={styles.tabs} role="group" aria-label="Documentos del lanzamiento">{plan.documents.map((doc, index) => <button key={doc.title} type="button" aria-pressed={selected === index} onClick={() => setSelected(index)}>{doc.title}</button>)}</div>
    <div className={styles.document} tabIndex={0} aria-label={plan.documents[selected]?.title}>{plan.documents[selected]?.text}</div>
    <div className={styles.review}><p>{plan.review.approved ? "Plan aprobado para preparación. Los anuncios y cualquier gasto requieren su aprobación operativa por separado." : "Aprueba esta versión del plan para continuar la preparación. Esta aprobación no activa anuncios, gasto, registros ni WhatsApp; los pendientes anteriores siguen vigentes."}</p>{!plan.review.approved ? <button type="button" disabled={Boolean(busy)} onClick={() => void approve()}>Aprobar plan · sin activar campañas</button> : <strong>Revisión guardada</strong>}</div>
    <h3>Vídeos de los anuncios</h3><p>Los textos ya están preparados. Guarda los MP4 en sus huecos para revisarlos. Esta carga conserva los archivos; el envío a Meta y la publicación requieren preparar y aprobar los anuncios con esos activos.</p>
    <div className={styles.slots}>{plan.video_slots.map(slot => <section key={slot.id} className={styles.slot}><h4>{slot.title}</h4><p>{slot.format}</p><p>{slot.copy}</p>{slot.uploaded ? <><strong>Vídeo guardado</strong><video controls preload="metadata" src={`${base}/api/v1/launch-plans/${encodeURIComponent(plan.slug)}/videos/${encodeURIComponent(slot.id)}?business_id=${encodeURIComponent(businessId)}`} /></> : <label className={styles.upload}>Añadir vídeo MP4<input type="file" accept="video/mp4,.mp4" disabled={Boolean(busy)} onChange={event => void upload(slot.id, event.target.files?.[0])} /></label>}</section>)}</div>
    {busy ? <p role="status">{busy === "review" ? "Guardando revisión…" : "Guardando vídeo…"}</p> : null}{message ? <p role="status">{message}</p> : null}
  </article>;
}

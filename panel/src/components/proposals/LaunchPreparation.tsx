import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { apiClient } from "@/api/client";
import { reviewSchema, type LaunchPlan } from "@/api/queries/launchPlans";
import { connectionTokenSchema, runtimeJobSchema, useRuntimeConnections } from "@/api/queries/runtime";
import { getAdsBasePath } from "@/utils/basePath";
import styles from "./LaunchPlans.module.css";
import { CampaignDrafts } from "./CampaignDrafts";

const labels = { queued: "Esperando runtime", running: "Preparando", prepared: "Borrador preparado", blocked: "Necesita información", failed: "Preparación interrumpida", cancelled: "Preparación cancelada" };

export function LaunchPreparation({ plan, businessId }: { plan: LaunchPlan; businessId: string }) {
  const client = useQueryClient();
  const connections = useRuntimeConnections(businessId);
  const [runtime, setRuntime] = useState<"codex" | "claude">("codex");
  const [token, setToken] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reply, setReply] = useState("");
  const job = plan.preparation;
  const connected = connections.data?.items.some(item => item.connected);

  async function act(operation: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true); setError(null);
    try {
      await operation();
      await Promise.all([client.invalidateQueries({ queryKey: ["launch-plans", businessId] }), client.invalidateQueries({ queryKey: ["runtime-connections", businessId] }), client.invalidateQueries({ queryKey: ["campaign-drafts", businessId] })]);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "No se pudo completar la operación."); }
    finally { setBusy(false); }
  }

  return <section className={styles.preparation} aria-label="Preparación y runtime">
    <div className={styles.summaryHeader}><h3>Preparación del plan</h3><span className={styles.status}>{job ? labels[job.state] : plan.review.approved ? "Preparación pendiente de iniciar" : "Pendiente de tu aprobación"}</span></div>
    <p>{job?.message ?? "Aprobar crea un encargo persistente. El runtime conectado lo recoge y devuelve un borrador real, con sus pendientes."}</p>
    {job?.state === "running" && job.lease_until && Date.parse(job.lease_until) < Date.now() ? <p role="status">Se ha perdido el contacto con el runtime. El trabajo se recuperará cuando vuelva a conectar.</p> : null}
    {job ? <p>Última actualización: <time dateTime={job.updated_at}>{new Date(job.updated_at).toLocaleString("es-ES")}</time> · Intentos: {job.attempts}</p> : null}
    {job?.result?.draft_id ? <details><summary>Ver borrador de campaña · revisión {job.result.draft_revision}</summary><CampaignDrafts businessId={businessId} draftId={job.result.draft_id} /></details> : null}
    {job?.result?.blockers.length ? <ul>{job.result.blockers.map(item => <li key={item}>{item}</li>)}</ul> : null}
    <p>Esto prepara borradores en el panel. No publica anuncios, activa gasto ni envía WhatsApp.</p>
    {job && ["blocked", "failed"].includes(job.state) ? <label className={styles.reply}>Respuesta para el runtime (opcional)<textarea value={reply} maxLength={4000} onChange={event => setReply(event.target.value)} placeholder="Añade los datos que faltan o una aclaración antes de reintentar." /></label> : null}
    <div className={styles.actions}>
      {plan.review.approved && !job ? <button disabled={busy} onClick={() => void act(() => apiClient.post(`/launch-plans/${plan.slug}/prepare`, reviewSchema, { revision: plan.revision }, { business_id: businessId }))}>Iniciar preparación del plan aprobado</button> : null}
      {job && ["blocked", "failed"].includes(job.state) ? <button disabled={busy} onClick={() => void act(() => apiClient.post(`/runtime/jobs/${job.id}/retry`, runtimeJobSchema, { message: reply }, { business_id: businessId }))}>Reintentar preparación</button> : null}
      {job && ["queued", "running", "blocked", "failed"].includes(job.state) ? <button disabled={busy} onClick={() => void act(() => apiClient.post(`/runtime/jobs/${job.id}/cancel`, runtimeJobSchema, {}, { business_id: businessId }))}>Cancelar preparación</button> : null}
    </div>
    <details className={styles.connector}>
      <summary>Conector con Codex / Claude Code · {connections.isError ? "No se pudo consultar" : connected ? "Conectado" : "Sin conector activo"}</summary>
      <p>El MCP no puede despertar un chat cerrado. Ejecuta el conector local para recibir encargos y devolver resultados aunque no estés escribiendo. Usa tu sesión del runtime y puede consumir su cuota; puedes detenerlo cuando quieras.</p>
      {connections.data?.items.filter(item => !item.revoked_at).map(item => <div className={styles.summaryHeader} key={item.id}><span>{item.label} · {item.connected ? "conectado" : "sin señal"}</span><button disabled={busy} onClick={() => void act(async () => { await apiClient.delete(`/runtime/connections/${item.id}`, z.object({ revoked: z.boolean() }), { business_id: businessId }); setToken(null); })}>Revocar acceso</button></div>)}
      <label>Runtime <select value={runtime} onChange={event => { setRuntime(event.target.value as "codex" | "claude"); setToken(null); }}><option value="codex">Codex</option><option value="claude">Claude Code</option></select></label>
      <button disabled={busy} onClick={() => void act(async () => { const result = await apiClient.post("/runtime/connections", connectionTokenSchema, { label: `${runtime} · conector local`, runtime }, { business_id: businessId }); setToken(result.token); })}>Crear acceso de preparación · 30 días</button>
      {token ? <div><p>Clave mostrada sólo ahora. No la pegues en un chat ni la subas a GitHub. Permite preparar borradores de este negocio, no gestionar anuncios.</p><label>Clave del conector <input readOnly type="password" value={token} autoComplete="off" /></label><button onClick={() => void navigator.clipboard.writeText(token).catch(() => setError("No se pudo copiar; selecciona la clave manualmente."))}>Copiar clave</button><button onClick={() => setToken(null)}>Ocultar clave</button></div> : null}
      <p>Desde el repositorio del motor, con el runtime instalado y autenticado:</p>
      <pre className={styles.command}>{`read -s SAFENT_RUNTIME_TOKEN\nexport SAFENT_RUNTIME_TOKEN\nuv run python -m safent_ads.runtime.bridge --url ${window.location.origin}${getAdsBasePath()} --runtime ${runtime}`}</pre>
      <p>El conector pide trabajos cada 30 segundos. Cerrar el proceso detiene la recepción; revocar el acceso corta también la autorización.</p>
    </details>
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}

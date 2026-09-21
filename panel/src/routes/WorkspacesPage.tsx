import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { useMe } from "@/api/queries/auth";
import { useWorkspaces, useWorkspace, useWorkspaceCommand, type Workspace, type WorkspaceResource } from "@/api/queries/workspaces";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { PageHeader } from "@/components/layout/PageHeader";
import { type CampaignDraft } from "@/api/queries/campaignDrafts";
import { useOfferings } from "@/api/queries/economics";
import { describeApiError } from "@/utils/apiError";
import styles from "./WorkspacesPage.module.css";

export function WorkspacesPage() {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  return <WorkspaceList key={businessId} businessId={businessId} />;
}

function WorkspaceList({ businessId }: { businessId: string }) {
  const query = useWorkspaces(businessId);
  const command = useWorkspaceCommand(businessId);
  const [title, setTitle] = useState("");
  const [key, setKey] = useState(() => `project-${crypto.randomUUID()}`);
  async function create(event: FormEvent) {
    event.preventDefault();
    if (!title.trim() || command.isPending) return;
    try { await command.mutateAsync({ path: "", body: { workspace_key: key, changes: { title: title.trim() } } }); setTitle(""); setKey(`project-${crypto.randomUUID()}`); } catch { /* Mutation error is displayed below. */ }
  }
  return <div className={styles.page}>
    <div className={styles.header}><PageHeader title="Trabajo" /><Link className={styles.button} to={`/propuestas?business_id=${businessId}`}>Decisiones pendientes</Link></div>
    <p className={styles.muted}>Un proyecto, un contexto y un historial. Continúa aquí, en Codex o en Claude Code.</p>
    <form className={styles.form} onSubmit={event => void create(event)}><label>Nuevo proyecto<input required maxLength={160} value={title} onChange={e => setTitle(e.target.value)} placeholder="Nombre del proyecto" /></label><div><button className={styles.button} disabled={!businessId || command.isPending}>Crear proyecto</button></div></form>
    {command.isError && <p role="alert">{describeApiError(command.error)}</p>}
    {query.isLoading && <p role="status">Cargando proyectos…</p>}
    {query.isError && <p role="alert">No se pudo cargar el trabajo. <button onClick={() => void query.refetch()}>Reintentar</button></p>}
    <div className={styles.grid}>{query.data?.items.map(item => <Link className={styles.card} key={item.id} to={`/trabajo/${item.id}?business_id=${businessId}`}>
      <span className={styles.status}>{item.campaign_count} campañas vinculadas</span><h2>{item.brief.title}</h2><p>{item.brief.objective || "Completa el objetivo y los datos del proyecto."}</p><span>Abrir proyecto →</span>
    </Link>)}</div>
    {query.data?.items.length === 0 && <p>Aún no hay proyectos. Los nuevos borradores también aparecerán aquí automáticamente.</p>}
    {query.data?.has_more && <p>Se muestran los 200 proyectos más recientes. Puedes abrir otro mediante su enlace directo.</p>}
  </div>;
}

export function WorkspacePage() {
  const { id = "" } = useParams();
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  const query = useWorkspace(businessId, id);
  return <div className={styles.page}>
    <Link to={`/trabajo?business_id=${businessId}`}>← Todos los proyectos</Link>
    {query.isLoading ? <p role="status">Cargando proyecto…</p> : query.isError ? <p role="alert">No se pudo cargar el proyecto. <button onClick={() => void query.refetch()}>Reintentar</button></p> : query.data ? <WorkspaceDetail key={`${businessId}:${id}`} workspace={query.data} /> : null}
  </div>;
}

const tabs = ["Resumen", "Campañas", "Materiales y seguimiento", "Actividad"] as const;
const activityLabels: Record<string, string> = { brief_saved: "Contexto actualizado", campaign_saved: "Borrador guardado", creation_proposed: "Propuesta de creación preparada" };
export function WorkspaceDetail({ workspace }: { workspace: Workspace }) {
  const [tab, setTab] = useState<typeof tabs[number]>("Resumen");
  const command = useWorkspaceCommand(workspace.business_id);
  const [notice, setNotice] = useState("");
  async function prepare(draftId: string, revision: number) {
    setNotice("");
    try { await command.mutateAsync({ path: `/${workspace.id}/prepare-campaign`, body: { draft_id: draftId, expected_revision: revision } }); setNotice("Propuesta preparada. Revisa el cambio exacto para autorizar la creación en pausa."); } catch { /* Visible error below. */ }
  }
  const suffix = `?business_id=${workspace.business_id}`;
  return <>
    <PageHeader title={workspace.brief.title} />
    <p className={styles.muted}>Contexto compartido · revisión {workspace.revision}. Guardar datos no autoriza cambios en plataformas.</p>
    <div className={styles.tabs} aria-label="Secciones del proyecto">{tabs.map(name => <button key={name} aria-pressed={tab === name} onClick={() => setTab(name)}>{name}</button>)}</div>
    {notice && <p role="status" className={styles.notice}>{notice}</p>}
    {command.isError && <p role="alert">{describeApiError(command.error)}</p>}
    {tab === "Resumen" && <>
      <div className={styles.notice}>Crear una campaña en pausa y activar anuncios son decisiones distintas. Los materiales pendientes no impiden preparar el contenedor.</div>
      <BriefEditor key={`${workspace.id}:${workspace.revision}`} workspace={workspace} />
      {workspace.brief.source_slug && <div className={styles.actions}><Link className={styles.button} to={`/propuestas/lanzamiento/${encodeURIComponent(workspace.brief.source_slug)}${suffix}`}>Ver plan, documentos y subir vídeos</Link></div>}
      <p className={styles.muted}>El presupuesto total es el marco de planificación. No acredita un límite aplicado en Meta o Google.</p>
    </>}
    {tab === "Campañas" && <>
      {workspace.campaigns.length === 0 && <p>No hay campañas vinculadas. Añade el primer borrador; Codex o Claude pueden completar sus datos desde el mismo proyecto.</p>}
      {workspace.campaigns.map(item => <section className={styles.card} key={item.draft.draft_id}>
        <h2>{item.draft.brief.title}</h2><p role="status">{item.step.label}</p>
        {item.proposal ? <>
          <p>Propuesta: {item.proposal.state}. {item.execution ? `Ejecución: ${item.execution.outcome}.` : "Todavía no hay un resultado de ejecución."}</p>
          {item.execution?.error_code && <p role="alert">{item.execution.error_code}</p>}
          {item.execution?.outcome === "SUCCEEDED" && <p>Identificador de campaña: {item.execution.created_external_id ?? "Consulta el recibo de ejecución"}</p>}
          <div className={styles.actions}><Link className={styles.button} to={`/propuestas${suffix}&proposal_id=${item.proposal.id}`}>Ver decisión y aprobar</Link><Link to={`/campanas${suffix}`}>Ver campañas reales</Link></div>
        </> : <>
          <CampaignEditor key={item.draft.revision} workspace={workspace} draft={item.draft} />
          <button className={styles.button} disabled={item.step.state !== "ready" || command.isPending} onClick={() => void prepare(item.draft.draft_id, item.draft.revision)}>{command.isPending ? "Preparando revisión…" : "Preparar revisión de creación en pausa"}</button>
        </>}
      </section>)}
      <NewCampaign workspace={workspace} />
    </>}
    {tab === "Materiales y seguimiento" && <>
      <p className={styles.muted}>Guiones, vídeos, landing, medición y WhatsApp comparten este registro. «Listo» es una declaración de preparación; no publica, envía ni verifica integraciones.</p>
      <ResourceEditor key={`${workspace.id}:${workspace.revision}`} workspace={workspace} />
      {workspace.brief.source_slug && <Link className={styles.button} to={`/propuestas/lanzamiento/${encodeURIComponent(workspace.brief.source_slug)}${suffix}`}>Abrir documentos y carga de vídeos del plan</Link>}
    </>}
    {tab === "Actividad" && <>
      <h2>Operaciones del proyecto</h2><ol className={styles.activity}>{workspace.activity.map(event => <li key={event.seq}>{activityLabels[event.kind] ?? event.kind} · {new Date(event.occurred_at).toLocaleString()}<br /><span className={styles.muted}>{event.actor}</span></li>)}</ol>
      {workspace.activity.length === 0 && <p>Sin nuevas operaciones registradas. Los borradores y propuestas existentes se han conservado.</p>}
      <h2>Preparación con asistentes</h2><p>El conector prepara borradores. Las operaciones de plataforma pasan por las aprobaciones del servidor.</p>
      {workspace.runtime_jobs.map(job => <article className={styles.card} key={job.id}><strong>{job.state}</strong><p>{job.message}</p></article>)}
      <p className={styles.muted}>La actividad muestra las últimas 100 operaciones y 20 encargos. No importa conversaciones personales de los asistentes.</p>
    </>}
  </>;
}

function BriefEditor({ workspace }: { workspace: Workspace }) {
  const command = useWorkspaceCommand(workspace.business_id);
  const [brief, setBrief] = useState(workspace.brief);
  async function save(event: FormEvent) {
    event.preventDefault();
    try { await command.mutateAsync({ path: "", body: { workspace_key: workspace.workspace_key, expected_revision: workspace.revision, changes: { title: brief.title, objective: brief.objective, schedule: brief.schedule, total_budget: brief.total_budget, notes: brief.notes } } }); } catch { /* Visible below. */ }
  }
  return <form className={styles.form} onSubmit={event => void save(event)}>
    <label>Nombre<input required maxLength={160} value={brief.title} onChange={e => setBrief({ ...brief, title: e.target.value })} /></label>
    <label>Objetivo<textarea maxLength={2000} value={brief.objective ?? ""} onChange={e => setBrief({ ...brief, objective: e.target.value || null })} /></label>
    <label>Fecha y horario confirmados<input maxLength={500} value={brief.schedule ?? ""} onChange={e => setBrief({ ...brief, schedule: e.target.value || null })} /></label>
    <label>Presupuesto total planificado (EUR)<input inputMode="decimal" pattern="[0-9]+([.][0-9]{1,2})?" value={brief.total_budget?.amount ?? ""} onChange={e => setBrief({ ...brief, total_budget: e.target.value ? { amount: e.target.value, currency: "EUR" } : null })} /></label>
    <label>Decisiones y contexto<textarea maxLength={8000} value={brief.notes ?? ""} onChange={e => setBrief({ ...brief, notes: e.target.value || null })} /></label>
    <div><button className={styles.button} disabled={command.isPending}>Guardar contexto compartido</button></div>
    {command.isError && <p role="alert">{describeApiError(command.error)} Recarga antes de sobrescribir una revisión más reciente.</p>}
  </form>;
}

function NewCampaign({ workspace }: { workspace: Workspace }) {
  const command = useWorkspaceCommand(workspace.business_id);
  const [title, setTitle] = useState("");
  const [key, setKey] = useState(() => `campaign-${crypto.randomUUID()}`);
  return <form className={styles.form} onSubmit={event => { event.preventDefault(); if (command.isPending) return; void command.mutateAsync({ path: `/${workspace.id}/campaigns`, body: { draft_key: key, changes: { title } } }).then(() => { setTitle(""); setKey(`campaign-${crypto.randomUUID()}`); }).catch(() => {}); }}>
    <label>Nuevo borrador de campaña<input required maxLength={128} value={title} onChange={e => setTitle(e.target.value)} /></label><div><button className={styles.button} disabled={command.isPending}>Añadir borrador al proyecto</button></div>{command.isError && <p role="alert">{describeApiError(command.error)}</p>}
  </form>;
}

const campaignFields = { objective: "Objetivo", success_criterion: "Cómo mediremos el éxito", kill_criterion: "Cuándo parar", angle: "Enfoque", targeting_seed: "Audiencia", geo: "Zona", notes: "Notas" };
function CampaignEditor({ workspace, draft }: { workspace: Workspace; draft: CampaignDraft }) {
  const command = useWorkspaceCommand(workspace.business_id);
  const offerings = useOfferings(workspace.business_id);
  const [changes, setChanges] = useState<Record<string, unknown>>({});
  const value = (key: string) => key in changes ? changes[key] : draft.brief[key];
  const set = (key: string, data: unknown) => setChanges(current => ({ ...current, [key]: data }));
  const budget = value("daily_budget") as { amount: string; currency: string } | null;
  return <details><summary>Completar datos de campaña ({draft.missing_fields.length} pendientes)</summary>
    <form className={styles.form} onSubmit={event => { event.preventDefault(); if (command.isPending) return; void command.mutateAsync({ path: `/${workspace.id}/campaigns`, body: { draft_key: draft.draft_key, expected_revision: draft.revision, changes } }).catch(() => {}); }}>
      <label>Nombre de campaña<input value={String(value("title") ?? "")} required maxLength={128} onChange={e => set("title", e.target.value)} /></label>
      <label>Cuenta publicitaria<select value={String(value("account_ref") ?? "")} onChange={e => { const account = workspace.accounts.find(a => a.account_ref === e.target.value); setChanges(current => ({ ...current, account_ref: account?.account_ref ?? null, platform: account?.platform ?? null })); }}><option value="">Seleccionar cuenta</option>{workspace.accounts.map(account => <option key={account.account_ref} value={account.account_ref} disabled={account.currency !== "EUR" || account.status !== "ACTIVE"}>{account.platform} · {account.external_account_id} · {account.currency} · {account.status}</option>)}</select></label>
      <label>Servicio u oferta<select value={String(value("offering_id") ?? "")} onChange={e => set("offering_id", e.target.value || null)}><option value="">Seleccionar oferta</option>{offerings.data?.items.map(item => <option key={item.offering_id} value={item.offering_id}>{item.title}</option>)}</select></label>
      {offerings.isError && <p role="alert">No se pudieron cargar las ofertas. <button type="button" onClick={() => void offerings.refetch()}>Reintentar</button></p>}
      <label>Presupuesto diario (EUR)<input inputMode="decimal" pattern="[0-9]+([.][0-9]{1,2})?" value={budget?.amount ?? ""} onChange={e => set("daily_budget", e.target.value ? { amount: e.target.value, currency: "EUR" } : null)} /></label>
      <label>Duración planificada (días)<input type="number" min={1} max={365} value={String(value("duration_days") ?? "")} onChange={e => set("duration_days", e.target.value ? Number(e.target.value) : null)} /></label>
      <label>URL de destino<input type="url" value={String(value("landing_url") ?? "")} onChange={e => set("landing_url", e.target.value || null)} /></label>
      {Object.entries(campaignFields).map(([key, label]) => <label key={key}>{label}<textarea maxLength={key === "notes" ? 4000 : key === "geo" ? 64 : 280} value={String(value(key) ?? "")} onChange={e => set(key, e.target.value || null)} /></label>)}
      <p className={styles.muted}>Estos datos preparan la revisión. La duración y la zona no acreditan una programación o segmentación aplicada.</p>
      <div><button className={styles.button} disabled={command.isPending || !Object.keys(changes).length}>Guardar datos de campaña</button></div>
      {command.isError && <p role="alert">{describeApiError(command.error)}</p>}
    </form>
  </details>;
}

function ResourceEditor({ workspace }: { workspace: Workspace }) {
  const command = useWorkspaceCommand(workspace.business_id);
  const [resources, setResources] = useState(workspace.brief.resources);
  function update(index: number, changes: Partial<WorkspaceResource>) { setResources(items => items.map((item, i) => i === index ? { ...item, ...changes } : item)); }
  return <form className={styles.form} onSubmit={event => { event.preventDefault(); void command.mutateAsync({ path: "", body: { workspace_key: workspace.workspace_key, expected_revision: workspace.revision, changes: { resources } } }).catch(() => {}); }}>
    {resources.map((resource, index) => <fieldset className={styles.card} key={resource.key} disabled={command.isPending}><legend>{resource.title || "Nuevo material"}</legend>
      <label>Nombre<input required maxLength={160} value={resource.title} onChange={e => update(index, { title: e.target.value })} /></label>
      <label>Tipo<select value={resource.kind} onChange={e => update(index, { kind: e.target.value as WorkspaceResource["kind"] })}>{["video", "script", "landing", "tracking", "whatsapp", "document"].map(kind => <option key={kind}>{kind}</option>)}</select></label>
      <label>Preparación<select value={resource.status} onChange={e => update(index, { status: e.target.value as WorkspaceResource["status"] })}><option value="pending">Pendiente</option><option value="in_progress">En curso</option><option value="ready">Listo para revisar</option></select></label>
      <label>Enlace HTTPS<input type="url" value={resource.url ?? ""} onChange={e => update(index, { url: e.target.value || null })} /></label>
      <label>Contenido o indicaciones<textarea maxLength={4000} value={resource.notes ?? ""} onChange={e => update(index, { notes: e.target.value || null })} /></label>
    </fieldset>)}
    <div className={styles.actions}><button className={styles.button} type="button" disabled={resources.length >= 100 || command.isPending} onClick={() => setResources([...resources, { key: crypto.randomUUID(), kind: "document", title: "", status: "pending", url: null, notes: null }])}>Añadir material o paso</button><button className={styles.button} disabled={command.isPending}>Guardar materiales</button></div>
    {command.isError && <p role="alert">{describeApiError(command.error)}</p>}
  </form>;
}

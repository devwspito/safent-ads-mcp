import { useEffect, useRef, useState, type ReactNode } from "react";
import { useEditCampaignCreation } from "@/api/queries/proposals";
import { useMe } from "@/api/queries/auth";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { ApiRequestError } from "@/api/client";
import { campaignCreationSchema, campaignCategories, campaignNetworks, campaignObjectives, politicalChoices, campaignPlanReady, type CampaignCreationPlan, type GoogleCampaignNative } from "@/api/schemas/campaignCreation";
import type { ProposalDetail } from "@/api/schemas/proposals";
import { campaignObjectiveLabel, googleBiddingStrategyLabel, googleChannelTypeLabel, googleConversionGoalsLabel, googleGeographicTargetingLabel } from "@/utils/proposals";
import { Modal } from "@/components/common/Modal";
import styles from "./CampaignCreationPanel.module.css";

const networkLabels = ["Búsqueda de Google", "Red de búsqueda", "Red de contenido", "Socios de búsqueda"];
const categoryLabels = ["Crédito", "Empleo", "Vivienda", "Temas sociales, elecciones o política", "Productos y servicios financieros"];
const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown) => typeof value === "string" ? value : "";

export function CampaignCreationPanel({ detail, stale, onReload }: { detail: ProposalDetail; stale: boolean; onReload: () => void }) {
  const [editing, setEditing] = useState(false);
  const ready = campaignPlanReady(detail);
  return <section className={styles.panel} aria-label="Plan de creación de campaña">
    <div className={styles.heading}><h3>Crear campaña</h3><span className={styles.status}>En pausa · sin publicación</span></div>
    <p className={styles.hint}>Este paso crea sólo la campaña y su presupuesto. No incluye grupos, anuncios, audiencias ni conversiones; activarla requiere otra decisión.</p>
    {/* Cuando el plan está listo, el resumen vive en el bloque compacto «Qué se crea» justo debajo (ProposalDetailPanel) — no se repite aquí. */}
    {!ready && <p role="alert" className={styles.error}>{detail.creation_plan_error === "campaign_creation_plan_required" ? "Falta un plan ejecutable. Completa los campos antes de aprobar." : "El plan no es válido o no se pudo verificar. Revisa su configuración antes de aprobar."}</p>}
    <button type="button" className={styles.secondary} disabled={stale || !["pending", "postponed", "approved", "scheduled"].includes(detail.state)} onClick={() => setEditing(true)}>{detail.creation_plan ? "Editar plan de creación" : "Completar plan de creación"}</button>
    {stale && <p role="status" className={styles.hint}>Actualizando la propuesta. Espera a que se verifique el plan.</p>}
    {editing && <CampaignCreationEditor key={detail.proposal_id} detail={detail} onClose={() => { setEditing(false); onReload(); }} onSaved={() => { setEditing(false); onReload(); }} />}
  </section>;
}

function GoogleNativeSummary({ native }: { native: GoogleCampaignNative }) {
  const geoLabel = googleGeographicTargetingLabel(native);
  const goalsLabel = googleConversionGoalsLabel(native);
  return <>
    <div><dt>Tipo y puja</dt><dd>{googleChannelTypeLabel(native.advertising_channel_type)} · {googleBiddingStrategyLabel(native)}</dd></div>
    <div><dt>Publicidad política UE</dt><dd>{native.contains_eu_political_advertising === politicalChoices[0] ? "Contiene publicidad política UE" : "No contiene publicidad política UE"}</dd></div>
    {native.advertising_channel_type === "SEARCH" ? campaignNetworks.map((key, index) => <div key={key}><dt>{networkLabels[index]}</dt><dd>{native.network_settings[key] ? "Activada" : "Desactivada"}</dd></div>) : null}
    {geoLabel ? <div><dt>Segmentación geográfica</dt><dd>{geoLabel}</dd></div> : null}
    {goalsLabel ? <div><dt>Conversiones objetivo</dt><dd>{goalsLabel}</dd></div> : null}
  </>;
}

export function PlanSummary({ plan }: { plan: CampaignCreationPlan }) {
  return <dl className={styles.summary}>
    <div><dt>Nombre</dt><dd>{plan.name}</dd></div><div><dt>Presupuesto diario</dt><dd>{plan.daily_budget.amount} EUR</dd></div>
    <div><dt>Estado inicial</dt><dd>PAUSED — no entrega anuncios</dd></div>
    {plan.platform === "google" ? <GoogleNativeSummary native={plan.native} /> : <>
      <div><dt>Objetivo</dt><dd>{campaignObjectiveLabel(plan.native.objective)}</dd></div>
      <div><dt>Compra y puja</dt><dd>Subasta · menor coste sin límite de puja · presupuesto en campaña</dd></div>
      <div><dt>Categorías especiales</dt><dd>{plan.native.special_ad_categories.map(value => categoryLabels[campaignCategories.indexOf(value)]).join(", ") || "Ninguna (declarado explícitamente)"}</dd></div>
      <div><dt>Países de categorías</dt><dd>{plan.native.special_ad_category_country.join(", ") || "Ninguno"}</dd></div>
    </>}
  </dl>;
}

function CampaignCreationEditor({ detail, onClose, onSaved }: { detail: ProposalDetail; onClose: () => void; onSaved: () => void }) {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  const mutation = useEditCampaignCreation(businessId);
  // Snapshot is intentional: background fetch must not rebase the approved edit.
  const [snapshot] = useState(() => ({ plan: record(detail.creation_plan), hash: detail.diff.diff_hash, proposalId: detail.proposal_id, platform: detail.platform }));
  const native = record(snapshot.plan.native), networks = record(native.network_settings);
  const [name, setName] = useState(text(snapshot.plan.name));
  const [budget, setBudget] = useState(record(snapshot.plan.daily_budget).currency === "EUR" ? text(record(snapshot.plan.daily_budget).amount) : "");
  const [political, setPolitical] = useState(text(native.contains_eu_political_advertising));
  const [networkValues, setNetworkValues] = useState<string[]>(() => campaignNetworks.map(key => typeof networks[key] === "boolean" ? networks[key] ? "yes" : "no" : ""));
  const [objective, setObjective] = useState(text(native.objective));
  const [categoryMode, setCategoryMode] = useState(Array.isArray(native.special_ad_categories) ? native.special_ad_categories.length ? "selected" : "none" : "");
  const [categories, setCategories] = useState<string[]>(Array.isArray(native.special_ad_categories) ? native.special_ad_categories.filter((value): value is string => typeof value === "string") : []);
  const [countries, setCountries] = useState(Array.isArray(native.special_ad_category_country) ? native.special_ad_category_country.filter(value => typeof value === "string").join(", ") : "");
  // Este editor sólo construye planes Google en Búsqueda con CPC manual (contrato original,
  // sin cambios): `geographic_targeting`/`conversion_goals` no tienen campo propio aquí, pero
  // si el plan guardado ya los trae se conservan tal cual — enseñarlos de sólo lectura y
  // reenviarlos es mejor que borrarlos en silencio al guardar (design.md §2.3).
  const rawGeographicConstants = Array.isArray(record(native.geographic_targeting).geo_target_constants)
    ? (record(native.geographic_targeting).geo_target_constants as unknown[]).filter((value): value is string => typeof value === "string")
    : [];
  const rawConversionGoals = Array.isArray(native.conversion_goals)
    ? native.conversion_goals.map(goal => text(record(goal).resource_name)).filter(Boolean)
    : [];
  const [review, setReview] = useState<CampaignCreationPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const submitted = useRef(false), alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  function prepareReview() {
    if (snapshot.platform === "meta" && (categoryMode === "selected" && !categories.length || !categoryMode)) {
      setError("Declara explícitamente ninguna categoría especial o selecciona al menos una."); return;
    }
    const plan = { schema_version: 1, platform: snapshot.platform, name, status: "PAUSED", daily_budget: { amount: budget.trim().replace(",", "."), currency: "EUR" }, native: snapshot.platform === "google" ? {
      advertising_channel_type: "SEARCH", bidding_strategy: "MANUAL_CPC", contains_eu_political_advertising: political,
      network_settings: Object.fromEntries(campaignNetworks.map((key, index) => [key, networkValues[index] === "" ? undefined : networkValues[index] === "yes"])),
      // Sin editor propio: se reenvían intactos si el plan guardado ya los traía, nunca se inventan.
      ...(native.geographic_targeting !== undefined ? { geographic_targeting: native.geographic_targeting } : {}),
      ...(native.conversion_goals !== undefined ? { conversion_goals: native.conversion_goals } : {}),
    } : { objective, buying_type: "AUCTION", bid_strategy: "LOWEST_COST_WITHOUT_CAP", special_ad_categories: categoryMode === "none" ? [] : categories, special_ad_category_country: countries.trim() ? countries.split(",").map(value => value.trim()) : [] } };
    const parsed = campaignCreationSchema.safeParse(plan);
    if (!parsed.success) { setError("Revisa nombre, presupuesto positivo (máximo 12 cifras enteras y dos decimales) y todas las elecciones de plataforma. Si hay categorías especiales, indica países ISO de dos letras mayúsculas sin duplicados."); return; }
    setError(null); setReview(parsed.data);
  }
  async function save() {
    if (!review || submitted.current || conflict) return;
    submitted.current = true; setBusy(true); setError(null);
    try {
      const result = await mutation.mutateAsync({ proposalId: snapshot.proposalId, diffHash: snapshot.hash, creationPlan: review });
      if (!alive.current) return;
      // An unchanged plan is a confirmed no-op: the server legitimately preserves its hash.
      if (!result.diff_hash) { setConflict(true); setError("No se pudo verificar el hash. Cierra y recarga antes de continuar."); return; }
      onSaved();
    } catch (failure) {
      if (!alive.current) return;
      if (failure instanceof ApiRequestError && failure.status === 409) { setConflict(true); setError("La propuesta cambió o tiene una ejecución pendiente. Conservamos este borrador; cierra y recarga la propuesta antes de volver a editar. No se ha aprobado nada."); }
      else setError("No se pudo guardar el plan. El borrador se conserva; comprueba la conexión y revisa la propuesta antes de reintentar.");
    } finally { submitted.current = false; if (alive.current) setBusy(false); }
  }
  const field = (label: string, children: ReactNode) => <label className={styles.field}>{label}{children}</label>;
  return <Modal label="Editar plan de creación" onClose={onClose} busy={busy} width="min(720px, 100%)">
    <div className={styles.editor}><h2>{review ? "Revisar plan de creación" : "Configurar campaña en pausa"}</h2>
      <p className={styles.hint}>{snapshot.platform === "google" ? "Google Search · CPC manual" : "Meta · subasta · menor coste sin límite"}. Cambiar el plan invalida aprobaciones anteriores. Guardar no aprueba ni publica.</p>
      <p className={styles.hint}>Sólo estructura de campaña y presupuesto. Sin grupos, conjuntos, anuncios, segmentación, conversiones ni fechas. El brief no configura estos campos. Activar la entrega requiere otra decisión.</p>
      {review ? <PlanSummary plan={review} /> : <fieldset disabled={busy} className={styles.fields}>
        {field("Nombre de campaña", <input autoFocus maxLength={128} value={name} onChange={event => setName(event.target.value)} />)}
        {field("Presupuesto diario (EUR)", <input inputMode="decimal" value={budget} onChange={event => setBudget(event.target.value)} placeholder="20.00" />)}
        <p className={styles.hint}>Moneda EUR · importe exacto, sin redondeo. Estado PAUSED fijo; no se activará la entrega.</p>
        {snapshot.platform === "google" ? <>
          {field("Publicidad política en la Unión Europea", <select aria-label="Publicidad política en la Unión Europea" value={political} onChange={event => setPolitical(event.target.value)}><option value="">Elige una declaración</option><option value={politicalChoices[0]}>Contiene publicidad política UE</option><option value={politicalChoices[1]}>No contiene publicidad política UE</option></select>)}
          <div className={styles.grid}>{campaignNetworks.map((key, index) => <label className={styles.field} key={key}>{networkLabels[index]}<select aria-label={networkLabels[index]} value={networkValues[index]} onChange={event => setNetworkValues(previous => previous.map((value, position) => position === index ? event.target.value : value))}><option value="">Elige una opción</option><option value="yes">Activada</option><option value="no">Desactivada</option></select></label>)}</div>
          {rawGeographicConstants.length > 0 && <p className={styles.hint}>Segmentación geográfica (no editable aquí, se conserva): {rawGeographicConstants.join(", ")}</p>}
          {rawConversionGoals.length > 0 && <p className={styles.hint}>Conversiones objetivo (no editable aquí, se conserva): {rawConversionGoals.join(", ")}</p>}
        </> : <>
          {field("Objetivo de Meta", <select aria-label="Objetivo de Meta" value={objective} onChange={event => setObjective(event.target.value)}><option value="">Elige un objetivo</option>{campaignObjectives.map((value) => <option key={value} value={value}>{campaignObjectiveLabel(value)}</option>)}</select>)}
          {field("Categorías especiales", <select aria-label="Categorías especiales" value={categoryMode} onChange={event => setCategoryMode(event.target.value)}><option value="">Elige una declaración</option><option value="none">Ninguna categoría especial</option><option value="selected">Se aplican categorías especiales</option></select>)}
          {categoryMode === "selected" && <fieldset className={styles.categories}><legend>Selecciona las categorías aplicables</legend>{campaignCategories.map((value, index) => <label key={value}><input type="checkbox" checked={categories.includes(value)} onChange={event => setCategories(previous => event.target.checked ? [...previous, value] : previous.filter(item => item !== value))} />{categoryLabels[index]}</label>)}</fieldset>}
          {field("Países de categorías especiales (ISO2)", <input value={countries} onChange={event => setCountries(event.target.value)} placeholder="ES, FR" />)}
          <p className={styles.hint}>Se comprueba el formato del código, no su disponibilidad: Meta debe validar los países y categorías para esta cuenta.</p>
        </>}
      </fieldset>}
      {error && <p className={styles.error} role="alert">{error}</p>}
      <div className={styles.actions}><button type="button" className={styles.secondary} disabled={busy} onClick={onClose}>Cerrar sin aprobar</button>
        {review ? <><button type="button" className={styles.secondary} disabled={busy} onClick={() => setReview(null)}>Volver a editar</button><button type="button" className={styles.primary} disabled={busy || conflict} onClick={() => void save()}>{busy ? "Guardando plan…" : "Guardar plan sin aprobar"}</button></> : <button type="button" className={styles.primary} disabled={conflict} onClick={prepareReview}>Revisar plan</button>}
      </div>
    </div>
  </Modal>;
}

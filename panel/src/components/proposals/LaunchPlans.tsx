import { Link } from "react-router-dom";
import { useLaunchPlans } from "@/api/queries/launchPlans";
import styles from "./LaunchPlans.module.css";

export function LaunchPlans({ businessId }: { businessId: string }) {
  const query = useLaunchPlans(businessId);
  if (query.isError) return <p role="alert">No se pudieron cargar los planes de lanzamiento. <button onClick={() => void query.refetch()}>Reintentar</button></p>;
  if (query.isLoading) return <p role="status">Cargando planes de lanzamiento…</p>;
  if (!query.data?.items.length) return null;
  return <section aria-label="Planes de lanzamiento" className={styles.section}>
    {query.data.items.map(plan => <Link
      key={plan.slug}
      to={`/propuestas/lanzamiento/${encodeURIComponent(plan.slug)}?business_id=${encodeURIComponent(businessId)}`}
      className={styles.summaryCard}
    >
      <div className={styles.summaryHeader}><span className={styles.eyebrow}>Plan de lanzamiento</span><span className={styles.status}>{plan.review.approved ? "Revisión guardada" : "Pendiente de revisión"}</span></div>
      <h2>{plan.title}</h2>
      <p>{plan.summary}</p>
      <div className={styles.summaryFooter}><span>{plan.documents.length} documentos · {plan.video_slots.filter(slot => slot.uploaded).length}/{plan.video_slots.length} vídeos guardados</span><span className={styles.detailLink}>Ver detalle <span aria-hidden="true">→</span></span></div>
    </Link>)}
  </section>;
}

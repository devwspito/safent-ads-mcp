import { Link, useParams } from "react-router-dom";
import { useMe } from "@/api/queries/auth";
import { useLaunchPlans } from "@/api/queries/launchPlans";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { PageHeader } from "@/components/layout/PageHeader";
import { LaunchPlanDetail } from "@/components/proposals/LaunchPlanDetail";
import styles from "@/components/proposals/LaunchPlans.module.css";

export function LaunchProposalPage() {
  const { slug } = useParams<{ slug: string }>();
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  const query = useLaunchPlans(businessId);
  const plan = query.data?.items.find(item => item.slug === slug);
  return <div>
    <Link className={styles.backLink} to={`/propuestas?business_id=${encodeURIComponent(businessId)}`}>← Volver a propuestas</Link>
    <PageHeader title="Detalle de la propuesta" />
    {!businessId || query.isLoading ? <p role="status">Cargando propuesta…</p>
      : query.isError ? <p role="alert">No se pudo cargar la propuesta. <button onClick={() => void query.refetch()}>Reintentar</button></p>
        : plan ? <LaunchPlanDetail key={`${businessId}:${plan.slug}`} plan={plan} businessId={businessId} />
          : <p role="status">Esta propuesta no está disponible en el negocio seleccionado.</p>}
  </div>;
}

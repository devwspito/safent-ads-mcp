import { Link, useParams } from "react-router-dom";
import { useMe } from "@/api/queries/auth";
import { usePackageDetail } from "@/api/queries/packages";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { PageHeader } from "@/components/layout/PageHeader";
import { PackagePreviewPanel } from "@/components/packages/PackagePreviewPanel";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { Skeleton } from "@/components/states/Skeleton";
import styles from "./PackagePreviewPage.module.css";

/**
 * Ruta de comprobación para T050/T051 (tasks.md, Bloque 5) — no es uno de los cuatro destinos
 * (design.md §1.1). Monta `PackagePreviewPanel` fuera de la fila de Propuestas hasta que T052
 * lo enganche ahí; sirve para probar el detalle contra `msw` y para las capturas de pantalla.
 */
const PACKAGE_IDS = ["pkg_meta_001", "pkg_google_001", "pkg_google_pmax_001", "pkg_meta_partial_demo"];

export function PackagePreviewIndexPage() {
  return (
    <div>
      <PageHeader title="Paquetes (comprobación)" />
      <ul className={styles.list}>
        {PACKAGE_IDS.map((packageId) => (
          <li key={packageId}>
            <Link to={`/propuestas/paquete/${packageId}`}>{packageId}</Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function PackagePreviewPage() {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  const { packageId } = useParams<{ packageId: string }>();
  const query = usePackageDetail(businessId, packageId ?? null);

  return (
    <div>
      <PageHeader title="Paquete (comprobación)" />
      <QueryBoundary
        isLoading={query.isLoading}
        isError={query.isError}
        error={query.error}
        onRetry={() => void query.refetch()}
        data={query.data}
        isEmpty={() => false}
        emptyTitle=""
        skeleton={
          <>
            <Skeleton height="280px" />
            <Skeleton height="120px" />
          </>
        }
      >
        {(detail) => (
          <>
            <h2 className={styles.campaignName}>{detail.campaign.name}</h2>
            <p className={styles.summaryLine}>
              {detail.platform.label} · {detail.platform.account.name}
            </p>
            <PackagePreviewPanel businessId={businessId} detail={detail} />
          </>
        )}
      </QueryBoundary>
    </div>
  );
}

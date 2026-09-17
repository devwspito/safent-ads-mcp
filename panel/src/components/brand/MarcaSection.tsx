import { useBrandDraft, useBrandKit } from "@/api/queries/brand";
import { ErrorState } from "@/components/states/ErrorState";
import { Skeleton } from "@/components/states/Skeleton";
import { BrandAssetUploadForm } from "./BrandAssetUploadForm";
import { BrandClaimsEditor } from "./BrandClaimsEditor";
import { BrandDraftReview } from "./BrandDraftReview";
import { BrandKitSummary } from "./BrandKitSummary";
import { WebsiteDiscoveryForm } from "./WebsiteDiscoveryForm";
import styles from "./MarcaSection.module.css";

interface MarcaSectionProps {
  businessId: string;
}

const MANUAL_UPLOAD_ANCHOR_ID = "brand-manual-upload";

/**
 * Sección "Marca" de Ajustes (rest-api.md §Marca, tool-surface.md §2.2 P1): kit confirmado arriba,
 * rastreo opcional del sitio web, borrador para revisar y confirmar, y el camino manual — todo
 * bajo el mismo negocio activo, sin ruta propia (panel-interaction-spec.md §1: ocho vistas fijas + Ajustes).
 */
export function MarcaSection({ businessId }: MarcaSectionProps) {
  const kitQuery = useBrandKit(businessId);
  const draftQuery = useBrandDraft(businessId);

  return (
    <section className={styles.section} aria-labelledby="marca-heading">
      <h2 id="marca-heading" className={styles.sectionTitle}>
        Marca
      </h2>

      {kitQuery.isLoading ? (
        <Skeleton height="96px" />
      ) : kitQuery.isError ? (
        <ErrorState message="No se pudo cargar el kit de marca." onRetry={() => void kitQuery.refetch()} />
      ) : (
        <BrandKitSummary kit={kitQuery.data ?? null} />
      )}

      {kitQuery.data ? <BrandClaimsEditor businessId={businessId} kit={kitQuery.data} /> : null}

      <WebsiteDiscoveryForm businessId={businessId} manualUploadAnchorId={MANUAL_UPLOAD_ANCHOR_ID} />

      {draftQuery.isError ? (
        <ErrorState message="No se pudo cargar el borrador de marca." onRetry={() => void draftQuery.refetch()} />
      ) : draftQuery.data ? (
        <BrandDraftReview businessId={businessId} draft={draftQuery.data} />
      ) : null}

      <BrandAssetUploadForm businessId={businessId} anchorId={MANUAL_UPLOAD_ANCHOR_ID} />
    </section>
  );
}

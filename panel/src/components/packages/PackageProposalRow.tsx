import { usePackageDetail } from "@/api/queries/packages";
import type { PackageFeedItem } from "@/api/schemas/proposals";
import type { PackageAdImage, PackagePreview } from "@/api/schemas/packages";
import { formatMoney } from "@/utils/money";
import { PlatformBadge } from "@/components/common/PlatformBadge";
import { PackagePreviewPanel } from "./PackagePreviewPanel";
import { PublicationProgress } from "./PublicationProgress";
import rowStyles from "@/components/proposals/ProposalGroupCard.module.css";
import styles from "./PackageProposalRow.module.css";

export interface PackageRowActions {
  onFocus: (packageId: string) => void;
  onToggleExpand: (packageId: string) => void;
  /** api.md §4 — exige la huella que vio el dueño, así que la fila la lee del detalle abierto. */
  onApprove: (item: PackageFeedItem, packageHash: string) => void;
  onReject: (item: PackageFeedItem) => void;
}

interface PackageProposalRowProps {
  businessId: string;
  item: PackageFeedItem;
  isFocused: boolean;
  isExpanded: boolean;
  writeDisabledReason: string | null;
  writeDisabledShortReason: string | null;
  actions: PackageRowActions;
}

/**
 * Fila de un paquete de campaña dentro de Propuestas — misma gramática que una propuesta
 * normal (design.md §2.2), discriminada por `item_kind === "package"` (tasks.md T052).
 * Mientras `state === "proposed"` se comporta como cualquier fila decidible; en cualquier otro
 * estado, el dinero y los botones ceden el sitio a `PublicationProgress` (T053) — la fila nunca
 * cambia de sitio ni abre modal, sólo cambia lo que hay a su derecha.
 */
export function PackageProposalRow({ businessId, item, isFocused, isExpanded, writeDisabledReason, writeDisabledShortReason, actions }: PackageProposalRowProps) {
  const isDecidable = item.state === "proposed";
  // Se pide siempre que sea decidible (no solo al expandir): la fila necesita el detalle para
  // enseñar la miniatura y los recuentos aunque esté colapsada (el feed no los trae, api.md §1/§2).
  const detail = usePackageDetail(businessId, isDecidable || isExpanded ? item.package_id : null);
  const preview = isDecidable && detail.data ? summarizePackagePreview(detail.data) : null;

  // `requires_expansion` es siempre `true` para un paquete (api.md §1): aprobar exige haber
  // abierto el detalle vigente y que su huella siga siendo la de esta fila (INV-8).
  const detailMatchesRow = Boolean(detail.data) && !detail.isFetching && !detail.isError && detail.data!.package_hash === item.diff_hash;
  const approveDisabled = Boolean(writeDisabledReason) || !isDecidable || !detailMatchesRow || (detailMatchesRow && !detail.data!.approvable);
  const reasonId = `package-disabled-reason-${item.package_id}`;

  const approveDisabledReason = writeDisabledShortReason
    ?? (!isDecidable
      ? null
      : !isExpanded
        ? "Abre el detalle para revisarlo antes de aprobar."
        : detail.isError
          ? "No se pudo cargar el detalle."
          : detail.isFetching || !detail.data
            ? null
            : !detailMatchesRow
              ? "El paquete cambió; revísalo de nuevo."
              : detail.data.not_approvable_reason);

  return (
    <>
      <div className={`${rowStyles.row} ${isFocused ? rowStyles.rowFocused : ""}`} data-package-id={item.package_id}>
        <div
          className={`${styles.content} pressable-row`}
          onClick={() => actions.onFocus(item.package_id)}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key !== "Enter" && event.key !== " ") return;
            event.preventDefault();
            actions.onFocus(item.package_id);
          }}
        >
          {preview?.thumbnail ? (
            <img className={styles.thumb} src={preview.thumbnail.preview_url} alt={preview.thumbnail.alt} width={40} height={40} loading="lazy" />
          ) : (
            <span className={styles.thumbPlaceholder} aria-hidden="true" />
          )}
          <div className={rowStyles.content}>
            <span className={rowStyles.what}>{item.headline}</span>
            <span className={styles.meta}>
              <PlatformBadge platform={item.platform} />
              {preview ? <span>{preview.summaryLine}</span> : null}
            </span>
            <span className={rowStyles.where}>{item.summary}</span>
            <span className={rowStyles.why}>{item.why}</span>
          </div>
        </div>

        {isDecidable ? (
          <>
            <span className={rowStyles.money}>
              {formatMoney(item.money.daily)}/día
              <br />
              {formatMoney(item.money.monthly_equivalent)}/mes
            </span>

            <div className={rowStyles.actions} onClick={(event) => event.stopPropagation()}>
              <button
                type="button"
                className={rowStyles.approveButton}
                disabled={approveDisabled}
                aria-describedby={approveDisabled && approveDisabledReason ? reasonId : undefined}
                aria-label={`Aprobar y publicar ${item.entity_name}`}
                onClick={() => detail.data && actions.onApprove(item, detail.data.package_hash)}
              >
                Aprobar y publicar
              </button>
              {approveDisabled && approveDisabledReason ? (
                <span id={reasonId} className={rowStyles.disabledReason}>
                  {approveDisabledReason}
                </span>
              ) : null}
              <div className={rowStyles.secondaryRow}>
                <button
                  type="button"
                  className={rowStyles.rejectButton}
                  disabled={Boolean(writeDisabledReason)}
                  aria-describedby={writeDisabledReason ? reasonId : undefined}
                  onClick={() => actions.onReject(item)}
                >
                  Rechazar
                </button>
              </div>
              <button
                type="button"
                className={rowStyles.detailToggle}
                aria-expanded={isExpanded}
                aria-label={isExpanded ? "Colapsar" : "Detalle"}
                onClick={() => actions.onToggleExpand(item.package_id)}
              >
                {isExpanded ? "Colapsar ⌃" : "Detalle ⌄"}
              </button>
            </div>
          </>
        ) : (
          <div className={rowStyles.actions} onClick={(event) => event.stopPropagation()}>
            <PublicationProgress businessId={businessId} packageId={item.package_id} />
            <button
              type="button"
              className={rowStyles.detailToggle}
              aria-expanded={isExpanded}
              aria-label={isExpanded ? "Colapsar" : "Detalle"}
              onClick={() => actions.onToggleExpand(item.package_id)}
            >
              {isExpanded ? "Colapsar ⌃" : "Detalle ⌄"}
            </button>
          </div>
        )}
      </div>

      {isExpanded ? <PackageDetailExpansion businessId={businessId} detail={detail} /> : null}
    </>
  );
}

interface PackageDetailExpansionProps {
  businessId: string;
  detail: ReturnType<typeof usePackageDetail>;
}

function PackageDetailExpansion({ businessId, detail }: PackageDetailExpansionProps) {
  if (detail.isError) {
    return (
      <div role="alert" className={rowStyles.disabledReason}>
        No se pudo cargar el detalle.{" "}
        <button type="button" onClick={() => void detail.refetch()}>
          Reintentar
        </button>
      </div>
    );
  }
  if (detail.isLoading || !detail.data) {
    return <p className={rowStyles.disabledReason}>Cargando detalle…</p>;
  }
  return <PackagePreviewPanel businessId={businessId} detail={detail.data} />;
}

interface PackageRowPreview {
  thumbnail: PackageAdImage | null;
  summaryLine: string;
}

function pluralize(count: number, singular: string, plural: string): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

/**
 * El feed de propuestas (`PackageFeedItem`, api.md §1) no trae imagen ni recuentos — solo el
 * `PackagePreview` completo los tiene. Se deriva aquí, en vez de ensanchar el esquema del feed
 * con datos que el servidor no manda todavía (checklists/panel-contract-followups.md).
 */
function summarizePackagePreview(detail: PackagePreview): PackageRowPreview {
  let ads = 0;
  let keywords = 0;
  let thumbnail: PackageAdImage | null = null;

  for (const adSet of detail.campaign.ad_sets) {
    ads += adSet.ads.length > 0 ? adSet.ads.length : adSet.asset_group ? 1 : 0;
    keywords += adSet.keywords_plain?.length ?? 0;
    if (!thumbnail) {
      thumbnail = adSet.ads.find((ad) => ad.image)?.image ?? adSet.asset_group?.images[0] ?? null;
    }
  }
  const publicos = detail.campaign.ad_sets.length;

  const segments = [
    ads > 0 ? pluralize(ads, "anuncio", "anuncios") : null,
    publicos > 0 ? pluralize(publicos, "público", "públicos") : null,
    keywords > 0 ? pluralize(keywords, "palabra clave", "palabras clave") : null,
  ].filter((segment): segment is string => Boolean(segment));

  return { thumbnail, summaryLine: segments.join(" · ") };
}

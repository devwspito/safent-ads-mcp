import { AdCard } from "./AdCard";
import { AssetGroupCard } from "./AssetGroupCard";
import { MoneyBlock } from "./MoneyBlock";
import { WhyBlock } from "./WhyBlock";
import type { PackageNativeSummaryItem, PackagePreview } from "@/api/schemas/packages";
import styles from "./PackagePreviewPanel.module.css";

interface PackagePreviewPanelProps {
  businessId: string;
  detail: PackagePreview;
}

const KEYWORDS_VISIBLE = 8;

/**
 * Etiquetas de `native_summary` que T036/T037 (contracts/panel.md §Canal, §Optimiza para)
 * sacan del array técnico y pintan SIEMPRE desplegadas — casilla 23 de threat-model.md §8,
 * mismo criterio que 003 ME-3. El resto del array (política UE, categorías) sigue exactamente
 * igual que hoy, plegado bajo «Ajustes que exige la plataforma» (contracts/api.md §7 punto 6):
 * Meta y Búsqueda no llevan ninguna de estas etiquetas, así que su panel no cambia un píxel.
 */
const CHANNEL_LABEL = "Canal";
const BIDDING_LABEL = "Tipo y puja";
const OPTIMIZES_FOR_LABEL = "Optimiza para";
const AUTOMATION_LABEL = "Automatización";
const SPEND_NOTICE_LABEL = "Aviso de gasto";
const ASSET_GROUP_COUNT_LABEL = "Grupos de recursos";

/** Labels que salen del array técnico y se pintan aparte — nunca las dos veces a la vez. */
const SURFACED_LABELS = new Set([
  CHANNEL_LABEL,
  BIDDING_LABEL,
  OPTIMIZES_FOR_LABEL,
  AUTOMATION_LABEL,
  SPEND_NOTICE_LABEL,
  ASSET_GROUP_COUNT_LABEL,
]);

function summaryValue(items: PackageNativeSummaryItem[], label: string): string | null {
  return items.find((item) => item.label === label)?.value ?? null;
}

/**
 * Detalle desplegado del paquete — `contracts/api.md` §7, orden fijo, sin excepciones:
 * 1) qué se va a publicar, 2) a quién, 3) dinero, 4) por qué, 5) qué pasará al aprobar,
 * 6) detalles de plataforma (plegado). El botón «Aprobar y publicar» vive en la fila de
 * Propuestas (T052), no aquí: este panel es sólo el detalle que se despliega debajo.
 */
export function PackagePreviewPanel({ businessId, detail }: PackagePreviewPanelProps) {
  const summary = detail.campaign.native_summary;
  const channel = summaryValue(summary, CHANNEL_LABEL);
  const bidding = summaryValue(summary, BIDDING_LABEL);
  const optimizesFor = summaryValue(summary, OPTIMIZES_FOR_LABEL);
  const automationNotice = summaryValue(summary, AUTOMATION_LABEL);
  const spendNotice = summaryValue(summary, SPEND_NOTICE_LABEL);
  const assetGroupCount = summaryValue(summary, ASSET_GROUP_COUNT_LABEL);
  // Sólo se saca del bloque técnico lo que de verdad se pinta aparte arriba (`channel` truthy).
  // Sin `Canal` (Meta y los paquetes de Búsqueda de siempre, T037 "sin cambios"), el `<details>`
  // sigue mostrando exactamente lo mismo de hoy, palabra por palabra.
  const hiddenFromDetails = channel ? SURFACED_LABELS : new Set<string>();

  return (
    <div className={styles.wrap}>
      {detail.not_approvable_reason ? (
        <p role="status" className={styles.notApprovable}>
          {detail.not_approvable_reason}
        </p>
      ) : null}

      {channel ? (
        <div className={styles.channelSummary}>
          <span className={styles.channelChip}>{channel}</span>
          {bidding ? <p className={styles.channelLine}>{bidding}</p> : null}
          {optimizesFor ? <p className={styles.channelLine}>Optimiza para: {optimizesFor}</p> : null}
          {assetGroupCount ? <p className={styles.channelLine}>{assetGroupCount}</p> : null}
          {automationNotice ? <p className={styles.channelLine}>{automationNotice}</p> : null}
          {spendNotice ? <p className={styles.channelNote}>{spendNotice}</p> : null}
        </div>
      ) : null}

      <section aria-labelledby="package-ads-heading" className={styles.section}>
        <h3 id="package-ads-heading" className={styles.sectionHeading}>
          Qué se va a publicar
        </h3>
        {detail.campaign.ad_sets.map((adSet) => (
          <div key={adSet.local_ref} className={styles.adSetBlock}>
            <p className={styles.adSetName}>
              {adSet.name}
              {adSet.asset_group ? <span className={styles.nodeLabel}> · {adSet.node_label}</span> : null}
            </p>
            {adSet.asset_group ? (
              <AssetGroupCard assetGroup={adSet.asset_group} />
            ) : (
              <div className={styles.adGrid}>
                {adSet.ads.map((ad) => (
                  <AdCard key={ad.local_ref} ad={ad} businessId={businessId} packageId={detail.package_id} packageHash={detail.package_hash} />
                ))}
              </div>
            )}
          </div>
        ))}
      </section>

      <section aria-labelledby="package-audience-heading" className={styles.section}>
        <h3 id="package-audience-heading" className={styles.sectionHeading}>
          A quién
        </h3>
        {detail.campaign.ad_sets.map((adSet) => {
          const keywords = adSet.keywords_plain;
          const visibleKeywords = keywords?.slice(0, KEYWORDS_VISIBLE) ?? [];
          const moreCount = keywords ? Math.max(0, keywords.length - KEYWORDS_VISIBLE) : 0;
          return (
            <div key={adSet.local_ref} className={styles.audienceBlock}>
              <p className={styles.audienceGroupName}>{adSet.name}</p>
              <p className={styles.audienceLine}>{adSet.audience_plain}</p>
              <p className={styles.audienceLine}>{adSet.geo_plain}</p>
              <p className={styles.audienceLine}>{adSet.schedule_plain}</p>
              {keywords ? (
                <p className={styles.audienceLine}>
                  {visibleKeywords.join(", ")}
                  {moreCount > 0 ? ` y ${moreCount} más` : ""}
                </p>
              ) : null}
              {adSet.keywords_note ? <p className={styles.audienceNote}>{adSet.keywords_note}</p> : null}
              {adSet.bid_plain ? <p className={styles.audienceLine}>{adSet.bid_plain}</p> : null}
            </div>
          );
        })}
      </section>

      <MoneyBlock money={detail.money} />
      <WhyBlock why={detail.why} />

      <section aria-labelledby="package-approve-heading" className={styles.section}>
        <h3 id="package-approve-heading" className={styles.sectionHeading}>
          Qué pasará al aprobar
        </h3>
        <p className={styles.approveSentence}>{detail.on_approve.sentence}</p>
        <p className={styles.undoSentence}>{detail.on_approve.undo_sentence}</p>
      </section>

      <details className={styles.nativeSummary}>
        <summary>Ajustes que exige la plataforma</summary>
        <dl className={styles.nativeSummaryList}>
          {detail.campaign.native_summary
            .filter((item) => !hiddenFromDetails.has(item.label))
            .map((item) => (
              <div key={item.label}>
                <dt>{item.label}</dt>
                <dd>{item.value}</dd>
              </div>
            ))}
        </dl>
      </details>
    </div>
  );
}

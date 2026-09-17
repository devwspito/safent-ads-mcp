import type { PackageAssetGroupPreview } from "@/api/schemas/packages";
import { isSafeUrl } from "@/utils/url";
import styles from "./AssetGroupCard.module.css";

interface AssetGroupCardProps {
  assetGroup: PackageAssetGroupPreview;
}

/**
 * Tarjeta del grupo de recursos — `contracts/panel.md`
 * §Grupo de recursos. En `PERFORMANCE_MAX` el grupo de recursos ES el anuncio (tasks.md T037):
 * sin esta tarjeta, un paquete sin `PlannedAd` dejaría «Qué se va a publicar» vacío, dando a
 * entender que no se va a publicar nada. Sólo lectura — a diferencia de `AdCard`, aquí no hay
 * «Cambiar imagen» ni «Regenerar» (fuera del alcance de T036/T037).
 */
export function AssetGroupCard({ assetGroup }: AssetGroupCardProps) {
  return (
    <figure className={styles.card}>
      <div className={styles.images}>
        {assetGroup.images.map((image) => (
          <img key={image.asset_id} className={styles.image} src={image.preview_url} alt={image.alt} width={image.width} height={image.height} loading="lazy" />
        ))}
      </div>

      <figcaption className={styles.body}>
        <p className={styles.businessName}>{assetGroup.business_name}</p>
        {assetGroup.headlines.map((headline, index) => (
          <p key={`headline-${index}`} className={styles.headline}>
            {headline}
          </p>
        ))}
        {assetGroup.long_headlines.map((headline, index) => (
          <p key={`long-headline-${index}`} className={styles.text}>
            {headline}
          </p>
        ))}
        {assetGroup.descriptions.map((description, index) => (
          <p key={`description-${index}`} className={styles.text}>
            {description}
          </p>
        ))}
        <p className={styles.signalCount}>
          {assetGroup.audience_signal_count > 0
            ? `${assetGroup.audience_signal_count} señales de público añadidas.`
            : "Sin señales de público añadidas."}
        </p>
        {isSafeUrl(assetGroup.final_url) ? (
          <a className={styles.landing} href={assetGroup.final_url} target="_blank" rel="noopener noreferrer">
            {assetGroup.final_url}
          </a>
        ) : (
          <span className={styles.landing}>{assetGroup.final_url}</span>
        )}
      </figcaption>
    </figure>
  );
}

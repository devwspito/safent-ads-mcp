import type { BrandKit } from "@/api/schemas/brand";
import { COLOR_ROLE_LABELS, describeIncompleteReason, isPlaceholderText } from "@/utils/brand";
import { formatExactDate } from "@/utils/time";
import { AssetPreviewThumb } from "./AssetPreviewThumb";
import styles from "./BrandKitSummary.module.css";

interface BrandKitSummaryProps {
  kit: BrandKit | null;
}

/** `GET /brand`: kit confirmado, o el motivo por el que sigue "incompleto" (rest-api.md §Marca). */
export function BrandKitSummary({ kit }: BrandKitSummaryProps) {
  if (kit === null) {
    return (
      <div className={styles.incomplete} role="status">
        <span className={styles.incompleteTitle}>Incompleto</span>
        <p className={styles.incompleteReason}>
          Todavía no hay ninguna identidad de marca para este negocio. Rastrea el sitio web o sube los activos a mano
          más abajo para empezar.
        </p>
      </div>
    );
  }

  const incompleteReason = describeIncompleteReason(kit);

  return (
    <div className={styles.kit}>
      <div className={styles.statusRow}>
        {!kit.is_confirmed ? <span className={styles.draftBadge}>borrador · sin confirmar</span> : null}
        {incompleteReason ? (
          <div className={styles.incomplete} role="status">
            <span className={styles.incompleteTitle}>Incompleto</span>
            <p className={styles.incompleteReason}>{incompleteReason}</p>
          </div>
        ) : null}
        <span className={styles.updatedAt}>Actualizado {formatExactDate(kit.updated_at)}</span>
      </div>

      <section className={styles.block}>
        <h3 className={styles.blockTitle}>Logotipos y activos</h3>
        {kit.assets.length === 0 ? (
          <p className={styles.emptyHint}>Sin activos confirmados.</p>
        ) : (
          <div className={styles.assetGrid}>
            {kit.assets.map((asset) => (
              <div key={asset.asset_id} className={styles.assetCard}>
                <AssetPreviewThumb
                  previewUrl={asset.preview_url}
                  alt={`Vista previa de ${asset.kind}`}
                  kind={asset.kind}
                />
                <span className={styles.assetUsage}>{isPlaceholderText(asset.usage) ? "Pendiente de revisar" : asset.usage}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className={styles.block}>
        <h3 className={styles.blockTitle}>Paleta</h3>
        {kit.palette.length === 0 ? (
          <p className={styles.emptyHint}>Sin paleta confirmada.</p>
        ) : (
          <div className={styles.paletteRow}>
            {kit.palette.map((swatch) => (
              <div key={swatch.role} className={styles.swatch}>
                <span className={styles.swatchColor} style={{ backgroundColor: swatch.hex }} aria-hidden="true" />
                <span className={styles.swatchRole}>{COLOR_ROLE_LABELS[swatch.role]}</span>
                <span className={styles.swatchHex}>{swatch.hex}</span>
                <span className={swatch.meets_wcag_aa_normal_text ? styles.contrastOk : styles.contrastWarn}>
                  Contraste {swatch.contrast_ratio_on_white.toFixed(2)}:1 {swatch.meets_wcag_aa_normal_text ? "· AA" : "· bajo AA"}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className={styles.block}>
        <h3 className={styles.blockTitle}>Tipografía</h3>
        <p className={isPlaceholderText(kit.typography.primary_family) ? styles.emptyHint : styles.text}>
          {isPlaceholderText(kit.typography.primary_family) ? "Pendiente de confirmar." : kit.typography.primary_family}
          {kit.typography.secondary_family ? ` · ${kit.typography.secondary_family}` : ""}
        </p>
        {kit.typography.weights.length > 0 ? <p className={styles.text}>Pesos: {kit.typography.weights.join(", ")}</p> : null}
        <p className={styles.text}>{isPlaceholderText(kit.typography.licence_note) ? "Licencia pendiente de confirmar." : kit.typography.licence_note}</p>
      </section>

      <section className={styles.block}>
        <h3 className={styles.blockTitle}>Tono de voz</h3>
        <p className={isPlaceholderText(kit.tone_of_voice.description) ? styles.emptyHint : styles.text}>
          {isPlaceholderText(kit.tone_of_voice.description) ? "Pendiente de confirmar." : kit.tone_of_voice.description}
        </p>
        {kit.tone_of_voice.adjectives.length > 0 ? <p className={styles.text}>Adjetivos: {kit.tone_of_voice.adjectives.join(", ")}</p> : null}
        {kit.tone_of_voice.avoid.length > 0 ? <p className={styles.text}>Evitar: {kit.tone_of_voice.avoid.join(", ")}</p> : null}
      </section>

      {kit.forbidden_claims.length > 0 ? (
        <section className={styles.block}>
          <h3 className={styles.blockTitle}>Afirmaciones prohibidas</h3>
          <ul className={styles.list}>
            {kit.forbidden_claims.map((entry) => (
              <li key={entry.claim}>
                {entry.claim}
                {entry.is_floor ? " · suelo, no editable" : ""}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {kit.legal_disclaimers.length > 0 ? (
        <section className={styles.block}>
          <h3 className={styles.blockTitle}>Avisos legales</h3>
          <ul className={styles.list}>
            {kit.legal_disclaimers.map((disclaimer) => (
              <li key={disclaimer.text}>
                {disclaimer.text}
                {disclaimer.applies_to ? ` (${disclaimer.applies_to.join(", ")})` : " (todas las plataformas)"}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

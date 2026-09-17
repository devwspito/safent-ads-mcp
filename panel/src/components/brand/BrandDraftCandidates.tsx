import type { BrandDraft, ColorRole } from "@/api/schemas/brand";
import { contrastRatioOnWhite, meetsWcagAaNormalText } from "@/utils/color";
import { isSafeUrl } from "@/utils/url";
import {
  ASSET_KIND_LABELS,
  COLOR_ROLE_LABELS,
  CONTACT_CHANNEL_LABELS,
  DISCOVERY_SOURCE_LABELS,
  SOCIAL_NETWORK_LABELS,
} from "@/utils/brand";
import { AssetPreviewThumb } from "./AssetPreviewThumb";
import styles from "./BrandDraftCandidates.module.css";

const COLOR_ROLE_OPTIONS: ColorRole[] = ["primary", "secondary", "accent", "background", "text"];

interface BrandDraftCandidatesProps {
  draft: BrandDraft;
  selectedAssetIds: Set<string>;
  onToggleAsset: (assetId: string) => void;
  paletteRoleByIndex: Map<number, ColorRole>;
  onSetPaletteRole: (index: number, role: ColorRole | null) => void;
  selectedTypographyFamily: string | null;
  onPickTypography: (family: string) => void;
}

function confidencePct(confidence: number): string {
  return `${Math.round(confidence * 100)} %`;
}

/** Candidatos de `GET /brand/draft` para revisión — cada uno seleccionable donde el contrato de
 * confirmación (`POST /brand/confirm`) admite referenciarlo; el resto es solo lectura. */
export function BrandDraftCandidates({
  draft,
  selectedAssetIds,
  onToggleAsset,
  paletteRoleByIndex,
  onSetPaletteRole,
  selectedTypographyFamily,
  onPickTypography,
}: BrandDraftCandidatesProps) {
  return (
    <div className={styles.candidates}>
      <section className={styles.section}>
        <h4 className={styles.sectionTitle}>Logotipos y otros activos ({draft.logo_candidates.length})</h4>
        {draft.logo_candidates.length === 0 ? (
          <p className={styles.emptyHint}>Sin candidatos todavía.</p>
        ) : (
          <ul className={styles.assetList}>
            {draft.logo_candidates.map((candidate) => {
              const inputId = `asset-${candidate.asset_id}`;
              return (
                <li key={candidate.asset_id} className={styles.assetItem}>
                  <input
                    id={inputId}
                    type="checkbox"
                    checked={selectedAssetIds.has(candidate.asset_id)}
                    onChange={() => onToggleAsset(candidate.asset_id)}
                  />
                  <AssetPreviewThumb
                    previewUrl={candidate.preview_url}
                    alt={`Vista previa: ${ASSET_KIND_LABELS[candidate.kind]}`}
                    kind={candidate.kind}
                  />
                  <label htmlFor={inputId} className={styles.assetLabel}>
                    <span className={styles.assetKind}>{ASSET_KIND_LABELS[candidate.kind]}</span>
                    <span className={styles.assetMeta}>
                      {DISCOVERY_SOURCE_LABELS[candidate.source]} · confianza {confidencePct(candidate.confidence)}
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className={styles.section}>
        <h4 className={styles.sectionTitle}>Colores candidatos ({draft.color_candidates.length})</h4>
        {draft.color_candidates.length === 0 ? (
          <p className={styles.emptyHint}>Sin candidatos todavía.</p>
        ) : (
          <ul className={styles.colorList}>
            {draft.color_candidates.map((candidate, index) => {
              const ratio = contrastRatioOnWhite(candidate.hex);
              const meetsAa = meetsWcagAaNormalText(ratio);
              const selectId = `palette-role-${index}`;
              const currentRole = paletteRoleByIndex.get(index) ?? "";
              return (
                <li key={`${candidate.hex}-${index}`} className={styles.colorItem}>
                  <span className={styles.colorSwatch} style={{ backgroundColor: candidate.hex }} aria-hidden="true" />
                  <div className={styles.colorMeta}>
                    <span className={styles.colorHex}>{candidate.hex}</span>
                    <span className={styles.assetMeta}>
                      {DISCOVERY_SOURCE_LABELS[candidate.source]} · confianza {confidencePct(candidate.confidence)}
                      {candidate.role_hint ? ` · sugerido: ${COLOR_ROLE_LABELS[candidate.role_hint]}` : ""}
                    </span>
                    <span className={meetsAa ? styles.contrastOk : styles.contrastWarn}>
                      Contraste calculado {ratio.toFixed(2)}:1 {meetsAa ? "· cumple AA" : "· no cumple AA"}
                    </span>
                  </div>
                  <label className={styles.selectLabel} htmlFor={selectId}>
                    Usar como
                    <select
                      id={selectId}
                      className={styles.select}
                      value={currentRole}
                      onChange={(event) => onSetPaletteRole(index, event.target.value ? (event.target.value as ColorRole) : null)}
                    >
                      <option value="">No usar</option>
                      {COLOR_ROLE_OPTIONS.map((role) => (
                        <option key={role} value={role}>
                          {COLOR_ROLE_LABELS[role]}
                        </option>
                      ))}
                    </select>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className={styles.section}>
        <h4 className={styles.sectionTitle}>Tipografías candidatas ({draft.typography_candidates.length})</h4>
        {draft.typography_candidates.length === 0 ? (
          <p className={styles.emptyHint}>Sin candidatos todavía.</p>
        ) : (
          <ul className={styles.typographyList}>
            {draft.typography_candidates.map((candidate) => {
              const inputId = `typography-${candidate.family}`;
              return (
                <li key={candidate.family} className={styles.typographyItem}>
                  <input
                    id={inputId}
                    type="radio"
                    name="typography-candidate"
                    checked={selectedTypographyFamily === candidate.family}
                    onChange={() => onPickTypography(candidate.family)}
                  />
                  <label htmlFor={inputId}>
                    <span className={styles.assetKind}>{candidate.family}</span>
                    <span className={styles.assetMeta}>
                      {DISCOVERY_SOURCE_LABELS[candidate.source]} · confianza {confidencePct(candidate.confidence)}
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className={styles.section}>
        <h4 className={styles.sectionTitle}>Nombre del negocio (candidatos)</h4>
        {draft.business_name_candidates.length === 0 ? (
          <p className={styles.emptyHint}>Sin candidatos todavía.</p>
        ) : (
          <>
            <p className={styles.emptyHint}>Solo informativo: el nombre del negocio no se cambia desde esta confirmación.</p>
            <ul className={styles.list}>
              {draft.business_name_candidates.map((candidate) => (
                <li key={candidate.name}>
                  {candidate.name} — {DISCOVERY_SOURCE_LABELS[candidate.source]} · confianza {confidencePct(candidate.confidence)}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      {draft.social_links.length > 0 ? (
        <section className={styles.section}>
          <h4 className={styles.sectionTitle}>Redes sociales</h4>
          <ul className={styles.list}>
            {draft.social_links.map((link) => (
              <li key={link.url}>
                {SOCIAL_NETWORK_LABELS[link.network]}:{" "}
                {isSafeUrl(link.url) ? (
                  <a href={link.url} target="_blank" rel="noopener noreferrer">
                    {link.url}
                  </a>
                ) : (
                  link.url
                )}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {draft.contact_channels.length > 0 ? (
        <section className={styles.section}>
          <h4 className={styles.sectionTitle}>Canales de contacto detectados</h4>
          <ul className={styles.list}>
            {draft.contact_channels.map((channel) => (
              <li key={channel.kind}>
                {CONTACT_CHANNEL_LABELS[channel.kind]} —{" "}
                {isSafeUrl(channel.page_url) ? (
                  <a href={channel.page_url} target="_blank" rel="noopener noreferrer">
                    ver página
                  </a>
                ) : (
                  channel.page_url
                )}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {draft.copy_samples.length > 0 ? (
        <section className={styles.section}>
          <h4 className={styles.sectionTitle}>Ejemplos de copy</h4>
          <ul className={styles.copyList}>
            {draft.copy_samples.map((sample) => (
              <li key={sample.text}>
                <blockquote className={styles.copyText}>{sample.text}</blockquote>
                <span className={styles.assetMeta}>{DISCOVERY_SOURCE_LABELS[sample.source]}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

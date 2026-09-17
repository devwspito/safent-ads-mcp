import { useState } from "react";
import { useConfirmBrandDraft } from "@/api/queries/brand";
import { ApiRequestError } from "@/api/client";
import type { BrandDraft, ColorRole } from "@/api/schemas/brand";
import { contrastRatioOnWhite } from "@/utils/color";
import { formatRelativeTime } from "@/utils/time";
import { BrandConfirmForm, type BrandConfirmFormFields } from "./BrandConfirmForm";
import { BrandDraftCandidates } from "./BrandDraftCandidates";
import styles from "./BrandDraftReview.module.css";

interface BrandDraftReviewProps {
  businessId: string;
  draft: BrandDraft;
}

const DEFAULT_ERROR_MESSAGE = "No se pudo confirmar la marca.";

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

/**
 * Revisión del borrador de marca (rest-api.md §Marca): "borrador · sin confirmar" mientras el
 * propietario no ha pasado por `POST /brand/confirm` — un rastreo o una subida nunca son la
 * fuente de verdad por sí solos.
 */
export function BrandDraftReview({ businessId, draft }: BrandDraftReviewProps) {
  const [selectedAssetIds, setSelectedAssetIds] = useState<Set<string>>(new Set());
  const [paletteRoleByIndex, setPaletteRoleByIndex] = useState<Map<number, ColorRole>>(new Map());
  const [primaryFont, setPrimaryFont] = useState("");
  const [confirmedNotice, setConfirmedNotice] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const confirmDraft = useConfirmBrandDraft(businessId);

  function toggleAsset(assetId: string) {
    setSelectedAssetIds((prev) => {
      const next = new Set(prev);
      if (next.has(assetId)) next.delete(assetId);
      else next.add(assetId);
      return next;
    });
  }

  function setPaletteRole(index: number, role: ColorRole | null) {
    setPaletteRoleByIndex((prev) => {
      const next = new Map(prev);
      if (role === null) next.delete(index);
      else next.set(index, role);
      return next;
    });
  }

  async function handleSubmit(fields: BrandConfirmFormFields) {
    setSubmitError(null);
    setConfirmedNotice(false);
    const palette = Array.from(paletteRoleByIndex.entries()).map(([index, role]) => {
      const candidate = draft.color_candidates[index]!;
      return { role, hex: candidate.hex, contrast_ratio_on_white: contrastRatioOnWhite(candidate.hex) };
    });
    try {
      await confirmDraft.mutateAsync({
        primary_font: primaryFont.trim(),
        font_licence_note: fields.fontLicenceNote.trim(),
        tone_description: fields.toneDescription.trim(),
        secondary_font: fields.secondaryFont.trim() || undefined,
        font_weights: splitList(fields.fontWeights),
        palette,
        tone_adjectives: splitList(fields.toneAdjectives),
        tone_avoid: splitList(fields.toneAvoid),
        selected_asset_ids: Array.from(selectedAssetIds),
      });
      setConfirmedNotice(true);
    } catch (error) {
      setSubmitError(error instanceof ApiRequestError ? error.message : DEFAULT_ERROR_MESSAGE);
    }
  }

  return (
    <div className={styles.review}>
      <div className={styles.header}>
        <span className={styles.draftBadge}>borrador · sin confirmar</span>
        <span className={styles.meta}>
          {draft.source_url ? `Rastreado de ${draft.source_url} · ` : "Subido a mano · "}
          {formatRelativeTime(draft.discovered_at)}
        </span>
      </div>

      <BrandDraftCandidates
        draft={draft}
        selectedAssetIds={selectedAssetIds}
        onToggleAsset={toggleAsset}
        paletteRoleByIndex={paletteRoleByIndex}
        onSetPaletteRole={setPaletteRole}
        selectedTypographyFamily={primaryFont || null}
        onPickTypography={setPrimaryFont}
      />

      <BrandConfirmForm
        primaryFont={primaryFont}
        onPrimaryFontChange={setPrimaryFont}
        isSubmitting={confirmDraft.isPending}
        submitError={submitError}
        onSubmit={(fields) => void handleSubmit(fields)}
      />

      {confirmedNotice ? (
        <span className={styles.confirmed} role="status">
          Marca confirmada.
        </span>
      ) : null}
    </div>
  );
}

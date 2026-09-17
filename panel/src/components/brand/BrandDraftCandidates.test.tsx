import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { BrandDraft } from "@/api/schemas/brand";
import { BrandDraftCandidates } from "./BrandDraftCandidates";

function emptyDraft(): BrandDraft {
  return {
    business_id: "biz_ejemplo",
    source_url: null,
    discovered_at: new Date().toISOString(),
    logo_candidates: [],
    color_candidates: [],
    typography_candidates: [],
    business_name_candidates: [],
    social_links: [],
    contact_channels: [],
    copy_samples: [],
  };
}

function noop() {
  return undefined;
}

describe("BrandDraftCandidates", () => {
  it("un candidato de logo se pinta con un <img> que usa preview_url, no storage_uri", () => {
    const draft: BrandDraft = {
      ...emptyDraft(),
      logo_candidates: [
        {
          asset_id: "asset_logo_1",
          kind: "logo_vector",
          storage_uri: "logo_vector/asset_logo_1.svg",
          preview_url: "/api/v1/brand/assets/asset_logo_1/preview",
          source: "inline_svg",
          confidence: 0.92,
        },
      ],
    };

    render(
      <BrandDraftCandidates
        draft={draft}
        selectedAssetIds={new Set()}
        onToggleAsset={noop}
        paletteRoleByIndex={new Map()}
        onSetPaletteRole={noop}
        selectedTypographyFamily={null}
        onPickTypography={noop}
      />,
    );

    const preview = screen.getByRole("img", { name: /Vista previa: Logotipo \(vector\)/ });
    expect(preview.tagName).toBe("IMG");
    expect(preview).toHaveAttribute("src", "/api/v1/brand/assets/asset_logo_1/preview");
  });

  it("sin candidatos, no hay ningún <img> de vista previa", () => {
    render(
      <BrandDraftCandidates
        draft={emptyDraft()}
        selectedAssetIds={new Set()}
        onToggleAsset={noop}
        paletteRoleByIndex={new Map()}
        onSetPaletteRole={noop}
        selectedTypographyFamily={null}
        onPickTypography={noop}
      />,
    );

    expect(screen.getByText("Logotipos y otros activos (0)")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});

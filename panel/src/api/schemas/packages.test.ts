import { describe, expect, it } from "vitest";
import { getPackageDetail } from "@/mocks/fixtures/packages";
import { packageCreativeCandidatesResponseSchema, packagePreviewSchema, patchPackageAdCreativeResponseSchema } from "./packages";

describe("packagePreviewSchema — contracts/api.md §2", () => {
  it("acepta el paquete de Meta (2 conjuntos × 2 anuncios con imagen)", () => {
    const detail = getPackageDetail("pkg_meta_001");
    const result = packagePreviewSchema.safeParse(detail);
    expect(result.success).toBe(true);
  });

  it("acepta el paquete de Google (1 grupo × 3 anuncios de texto con palabras clave)", () => {
    const detail = getPackageDetail("pkg_google_001");
    const result = packagePreviewSchema.safeParse(detail);
    expect(result.success).toBe(true);
  });

  it("no rompe si el servidor añade un campo nuevo — tolerante, nunca `extra: forbid`", () => {
    const detail = getPackageDetail("pkg_meta_001");
    const result = packagePreviewSchema.safeParse({ ...detail, unexpected_future_field: "algo que el panel todavía no conoce" });
    expect(result.success).toBe(true);
  });

  it("un `ad_set` con un campo nuevo tampoco rompe el paquete", () => {
    const detail = getPackageDetail("pkg_meta_001")!;
    const withExtra = {
      ...detail,
      campaign: {
        ...detail.campaign,
        ad_sets: detail.campaign.ad_sets.map((adSet, index) => (index === 0 ? { ...adSet, future_targeting_mode: "lookalike_v2" } : adSet)),
      },
    };
    expect(packagePreviewSchema.safeParse(withExtra).success).toBe(true);
  });

  it("rechaza un paquete sin `package_hash` — es obligatorio para aprobar (INV-8)", () => {
    const withoutHash: Record<string, unknown> = { ...getPackageDetail("pkg_meta_001")! };
    delete withoutHash.package_hash;
    expect(packagePreviewSchema.safeParse(withoutHash).success).toBe(false);
  });

  it("acepta `why.research` nulo — el panel pinta «Sin datos de competencia»", () => {
    const detail = getPackageDetail("pkg_google_001")!;
    expect(detail.why.research).toBeNull();
    expect(packagePreviewSchema.safeParse(detail).success).toBe(true);
  });

  it("acepta un paquete de Máximo Rendimiento — un grupo de recursos, `ads: []` (tasks.md T037)", () => {
    const detail = getPackageDetail("pkg_google_pmax_001")!;
    const result = packagePreviewSchema.safeParse(detail);
    expect(result.success).toBe(true);
    expect(detail.campaign.ad_sets[0]!.ads).toEqual([]);
    expect(detail.campaign.ad_sets[0]!.asset_group).not.toBeNull();
  });

  it("un `ad_set` sin `node_label` ni `asset_group` (servidor anterior a T036) usa los valores por defecto", () => {
    const detail = getPackageDetail("pkg_meta_001")!;
    const withoutNewFields = {
      ...detail,
      campaign: {
        ...detail.campaign,
        ad_sets: detail.campaign.ad_sets.map((adSet) => {
          const rest: Record<string, unknown> = { ...adSet };
          delete rest.node_label;
          delete rest.asset_group;
          return rest;
        }),
      },
    };
    const result = packagePreviewSchema.safeParse(withoutNewFields);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.campaign.ad_sets[0]!.node_label).toBe("Conjunto de anuncios");
      expect(result.data.campaign.ad_sets[0]!.asset_group).toBeNull();
    }
  });
});

describe("packageCreativeCandidatesResponseSchema y patchPackageAdCreativeResponseSchema — §6", () => {
  it("acepta una lista vacía de candidatos", () => {
    expect(packageCreativeCandidatesResponseSchema.safeParse({ items: [] }).success).toBe(true);
  });

  it("acepta la respuesta del PATCH con la huella nueva", () => {
    expect(patchPackageAdCreativeResponseSchema.safeParse({ package_hash: "pkgh_abc123" }).success).toBe(true);
  });
});

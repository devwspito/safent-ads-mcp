import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { PackageAssetGroupPreview } from "@/api/schemas/packages";
import { AssetGroupCard } from "./AssetGroupCard";

function buildAssetGroup(overrides: Partial<PackageAssetGroupPreview> = {}): PackageAssetGroupPreview {
  return {
    business_name: "Clínica X",
    images: [
      { preview_url: "/img/logo.png", width: 1080, height: 1080, alt: "Logo", asset_id: "a1", policy: "ok" },
      { preview_url: "/img/marketing.png", width: 1200, height: 628, alt: "Imagen de marketing", asset_id: "a2", policy: "ok" },
      { preview_url: "/img/square.png", width: 1080, height: 1080, alt: "Imagen cuadrada", asset_id: "a3", policy: "ok" },
    ],
    headlines: ["Reserva ya", "Cita veterinaria", "Atención 24h"],
    long_headlines: ["Reserva tu cita veterinaria en minutos"],
    descriptions: ["Reserva tu cita en minutos", "Atención profesional cercana"],
    audience_signal_count: 0,
    final_url: "https://negocio-ejemplo.es/reservar",
    ...overrides,
  };
}

describe("AssetGroupCard — tasks.md T037", () => {
  it("pinta las tres imágenes, los textos y la URL completa sin acortar", () => {
    render(<AssetGroupCard assetGroup={buildAssetGroup()} />);

    expect(screen.getAllByRole("img")).toHaveLength(3);
    expect(screen.getByText("Reserva ya")).toBeInTheDocument();
    expect(screen.getByText("Reserva tu cita veterinaria en minutos")).toBeInTheDocument();
    expect(screen.getByText("Reserva tu cita en minutos")).toBeInTheDocument();
    expect(screen.getByText("Clínica X")).toBeInTheDocument();
    expect(screen.getByText("https://negocio-ejemplo.es/reservar")).toBeInTheDocument();
  });

  it("sin señales de público añadidas declara el recuento en cero, nunca un hueco vacío", () => {
    render(<AssetGroupCard assetGroup={buildAssetGroup()} />);

    expect(screen.getByText("Sin señales de público añadidas.")).toBeInTheDocument();
  });

  it("no usa jerga de proveedor («asset group», «PMax») en ningún texto visible", () => {
    render(<AssetGroupCard assetGroup={buildAssetGroup()} />);

    const forbidden = ["asset group", "pmax", "opt-out"];
    const text = document.body.textContent?.toLowerCase() ?? "";
    forbidden.forEach((word) => expect(text).not.toContain(word));
  });
});

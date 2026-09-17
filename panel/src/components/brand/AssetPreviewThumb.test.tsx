import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { AssetPreviewThumb } from "./AssetPreviewThumb";

describe("AssetPreviewThumb", () => {
  it("usa preview_url como src cuando el tipo de activo es una imagen", () => {
    render(<AssetPreviewThumb previewUrl="/api/v1/brand/assets/asset_logo_1/preview" alt="Vista previa: Logotipo" kind="logo_vector" />);

    const img = screen.getByRole("img", { name: "Vista previa: Logotipo" });
    expect(img.tagName).toBe("IMG");
    expect(img).toHaveAttribute("src", "/api/v1/brand/assets/asset_logo_1/preview");
  });

  it("cae a la vista de reserva para un tipo que no es imagen (font_file), sin intentar un <img>", () => {
    render(<AssetPreviewThumb previewUrl="/api/v1/brand/assets/asset_font_1/preview" alt="Vista previa: Tipografía" kind="font_file" />);

    expect(screen.queryByRole("img", { name: "Vista previa: Tipografía" })?.tagName).not.toBe("IMG");
    expect(screen.getByText("Tipografía (archivo)")).toBeInTheDocument();
  });

  it("cae a la vista de reserva si la carga de la imagen falla", () => {
    render(<AssetPreviewThumb previewUrl="/api/v1/brand/assets/asset_logo_1/preview" alt="Vista previa: Logotipo" kind="logo_vector" />);

    const img = screen.getByRole("img", { name: "Vista previa: Logotipo" });
    fireEvent.error(img);

    expect(screen.getByText("Logotipo (vector)")).toBeInTheDocument();
  });
});

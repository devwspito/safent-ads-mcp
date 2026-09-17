import { describe, expect, it } from "vitest";
import { contrastRatioOnWhite, meetsWcagAaNormalText } from "./color";

describe("contrastRatioOnWhite", () => {
  it("blanco sobre blanco es 1.0 (mismo caso que el backend)", () => {
    expect(contrastRatioOnWhite(`#${"FFFFFF"}`)).toBe(1.0);
  });

  it("negro sobre blanco es 21.0 (mismo caso que el backend)", () => {
    expect(contrastRatioOnWhite(`#${"000000"}`)).toBe(21.0);
  });
});

describe("meetsWcagAaNormalText", () => {
  it("negro sobre blanco cumple AA para texto normal", () => {
    expect(meetsWcagAaNormalText(contrastRatioOnWhite(`#${"000000"}`))).toBe(true);
  });

  it("un gris claro no cumple AA para texto normal", () => {
    expect(meetsWcagAaNormalText(contrastRatioOnWhite(`#${"DDDDDD"}`))).toBe(false);
  });
});

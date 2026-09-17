import { describe, expect, it } from "vitest";
import { toCsv } from "./csv";

describe("toCsv", () => {
  it("genera cabecera y filas en el orden de columnas indicado", () => {
    const csv = toCsv([{ a: 1, b: "x" }, { a: 2, b: "y" }], ["a", "b"]);
    expect(csv).toBe("a,b\n1,x\n2,y");
  });

  it("escapa comas, comillas y saltos de línea (RFC 4180)", () => {
    const csv = toCsv([{ resumen: 'Subió 90 € → 117 €, motivo: "CPL bajo"\nnueva línea' }], ["resumen"]);
    expect(csv).toBe('resumen\n"Subió 90 € → 117 €, motivo: ""CPL bajo""\nnueva línea"');
  });

  it("trata los valores ausentes como cadena vacía", () => {
    const csv = toCsv([{ a: 1 }], ["a", "b"]);
    expect(csv).toBe("a,b\n1,");
  });
});

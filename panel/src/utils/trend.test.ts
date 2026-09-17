import { describe, expect, it } from "vitest";
import { aggregateSeries, seriesTrend } from "./trend";

describe("aggregateSeries", () => {
  it("suma día a día varias series de la misma longitud", () => {
    expect(aggregateSeries([[1, 2, 3], [10, 20, 30]])).toEqual([11, 22, 33]);
  });

  it("una lista vacía de series no inventa puntos", () => {
    expect(aggregateSeries([])).toEqual([]);
  });
});

describe("seriesTrend", () => {
  const fourteen = [10, 10, 10, 10, 10, 10, 10, 20, 20, 20, 20, 20, 20, 20];

  it("7 y 14 días parten la serie de 14 puntos por la mitad para comparar", () => {
    expect(seriesTrend(fourteen, "7D").deltaPct).toBe(100);
    expect(seriesTrend(fourteen, "14D").deltaPct).toBe(100);
  });

  it("30 días nunca inventa una diferencia", () => {
    expect(seriesTrend(fourteen, "30D").deltaPct).toBeNull();
  });

  it("sin gasto previo no divide entre cero", () => {
    expect(seriesTrend([0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1], "7D").deltaPct).toBeNull();
  });
});

import { describe, expect, it } from "vitest";
import { projectedMonthEndSpend } from "./money";

describe("projectedMonthEndSpend", () => {
  it("extrapola el gasto en lo que va de mes por los días transcurridos", () => {
    // Día 10 de un mes de 30 días, 300 € gastados -> ritmo de 30 €/día -> 900 € a fin de mes.
    const today = new Date(2026, 3, 10); // abril, 30 días
    expect(projectedMonthEndSpend({ amount: 300, currency: "EUR" }, today)).toEqual({ amount: 900, currency: "EUR" });
  });
});

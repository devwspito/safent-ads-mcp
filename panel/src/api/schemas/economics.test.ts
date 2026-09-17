import { describe, expect, it } from "vitest";
import {
  conversionsImportResultSchema,
  offeringEconomicsInputSchema,
  offeringSchema,
  offeringsResponseSchema,
  webhookTokenResponseSchema,
} from "./economics";

const VALID_OFFERING = {
  offering_id: "off_plan_anual",
  code: "plan-anual",
  title: "Plan Anual Pro",
  is_active: true,
  list_price: { amount: 1200, currency: "EUR" },
  economics: {
    vat_rate_pct: 21,
    delivery_cost_minor: 9_000,
    sales_cost_minor: 14_000,
    refund_rate_pct: 6,
    payment_plan: "none",
    currency: "EUR",
    updated_at: "2026-09-10T10:00:00Z",
  },
};

describe("offeringSchema", () => {
  it("acepta una oferta con economía rellena", () => {
    expect(offeringSchema.safeParse(VALID_OFFERING).success).toBe(true);
  });

  it("acepta una oferta provisional (economics null)", () => {
    expect(offeringSchema.safeParse({ ...VALID_OFFERING, economics: null }).success).toBe(true);
  });

  it("acepta refund_rate_pct null dentro de economics", () => {
    const result = offeringSchema.safeParse({
      ...VALID_OFFERING,
      economics: { ...VALID_OFFERING.economics, refund_rate_pct: null },
    });
    expect(result.success).toBe(true);
  });

  it("rechaza un payment_plan fuera de none|instalments", () => {
    const result = offeringSchema.safeParse({
      ...VALID_OFFERING,
      economics: { ...VALID_OFFERING.economics, payment_plan: "otra_cosa" },
    });
    expect(result.success).toBe(false);
  });
});

describe("offeringsResponseSchema", () => {
  it("envuelve la lista en items", () => {
    expect(offeringsResponseSchema.safeParse({ items: [VALID_OFFERING] }).success).toBe(true);
  });
});

describe("offeringEconomicsInputSchema", () => {
  const VALID_INPUT = {
    vat_rate_pct: 21,
    delivery_cost_minor: 9_000,
    sales_cost_minor: 14_000,
    refund_rate_pct: 6,
    payment_plan: "none",
    currency: "EUR",
  };

  it("acepta un input válido", () => {
    expect(offeringEconomicsInputSchema.safeParse(VALID_INPUT).success).toBe(true);
  });

  it("acepta refund_rate_pct null", () => {
    expect(offeringEconomicsInputSchema.safeParse({ ...VALID_INPUT, refund_rate_pct: null }).success).toBe(true);
  });

  it.each([
    ["vat_rate_pct por debajo de 0", { vat_rate_pct: -1 }],
    ["vat_rate_pct por encima de 100", { vat_rate_pct: 100.5 }],
    ["refund_rate_pct por encima de 100", { refund_rate_pct: 101 }],
    ["delivery_cost_minor negativo", { delivery_cost_minor: -1 }],
    ["sales_cost_minor negativo", { sales_cost_minor: -1 }],
    ["currency de más de 3 letras", { currency: "EURO" }],
  ])("rechaza %s", (_label, overrides) => {
    const result = offeringEconomicsInputSchema.safeParse({ ...VALID_INPUT, ...overrides });
    expect(result.success).toBe(false);
  });
});

describe("conversionsImportResultSchema", () => {
  it("acepta el resumen con filas rechazadas", () => {
    const result = conversionsImportResultSchema.safeParse({
      imported: 2,
      duplicates: 1,
      rejected: [{ line: 5, reason: "kind desconocido" }],
    });
    expect(result.success).toBe(true);
  });
});

describe("webhookTokenResponseSchema", () => {
  it("acepta { token }", () => {
    expect(webhookTokenResponseSchema.safeParse({ token: "whk_abc123" }).success).toBe(true);
  });
});

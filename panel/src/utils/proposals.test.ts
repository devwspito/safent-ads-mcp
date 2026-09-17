import { describe, expect, it } from "vitest";
import { classificationLabel, creationDiffPayload, describeProposalChange, expiresWithin24h, expiryCountdownLabel, proposalDailyMoney, proposalDisplayName, proposalStateLabel, riskLevelLabel, urgencyLabel } from "./proposals";
import { formatMoney } from "./money";

/**
 * Mismo payload real que sirve `panel_read.py::proposal_item` para una creación: el plan de
 * creación (y el destino guardado) van serializados en `diff.valor_propuesto` — companion 0.2.20.
 */
const REAL_ACCOUNT_REF = "google:account:5e1a6c8e-2f3d-4b7a-9c1e-8f2b6a7d4c10:9b3f2a71-6d4c-4e8a-b1f0-7c5e3a9d2f44:1000000001";
const REAL_CREATION_PLAN = {
  schema_version: 1,
  platform: "google",
  name: "Acme | Primera consulta gratis | Centro Norte",
  status: "PAUSED",
  daily_budget: { amount: "10", currency: "EUR" },
  native: {
    advertising_channel_type: "SEARCH",
    bidding_strategy: "MANUAL_CPC",
    contains_eu_political_advertising: "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    network_settings: { target_google_search: true, target_search_network: false, target_content_network: false, target_partner_search_network: false },
  },
};
const REAL_CREATION_DIFF = {
  parametro: "new_campaign:google",
  valor_actual: "null",
  valor_propuesto: JSON.stringify({ creation_plan: REAL_CREATION_PLAN, landing_url: "https://example.com/" }),
  diff_hash: "h",
};

describe("urgencyLabel/classificationLabel/riskLevelLabel", () => {
  it("traduce los enums en inglés del servidor a etiquetas en español", () => {
    expect(urgencyLabel("critical")).toBe("Crítico");
    expect(classificationLabel("routine")).toBe("Rutinaria");
    expect(riskLevelLabel("high")).toBe("Riesgo alto");
  });
});

describe("proposalStateLabel", () => {
  it("traduce un estado conocido", () => {
    expect(proposalStateLabel("postponed")).toBe("Pospuesta");
  });

  it("devuelve el valor crudo si el estado es desconocido, sin lanzar", () => {
    expect(proposalStateLabel("unknown_future_state")).toBe("unknown_future_state");
  });
});

describe("expiryCountdownLabel", () => {
  const now = new Date("2026-09-09T12:00:00.000Z");

  it("cuenta hacia adelante cuando no ha caducado", () => {
    expect(expiryCountdownLabel("2026-09-09T15:00:00.000Z", now)).toBe("caduca en 3 h");
  });

  it("cuenta hacia atrás cuando ya caducó", () => {
    expect(expiryCountdownLabel("2026-09-09T11:00:00.000Z", now)).toBe("caducada hace 1 h");
  });
});

describe("expiresWithin24h", () => {
  const now = new Date("2026-09-09T12:00:00.000Z");

  it("es verdad con menos de 24 h por delante", () => {
    expect(expiresWithin24h("2026-09-10T11:00:00.000Z", now)).toBe(true);
  });

  it("es falso con 24 h o más por delante", () => {
    expect(expiresWithin24h("2026-09-10T13:00:00.000Z", now)).toBe(false);
  });
});

describe("describeProposalChange", () => {
  it("crear campaña: usa el nombre del plan de creación, nunca la referencia cruda de la cuenta (defecto companion 0.2.20)", () => {
    expect(
      describeProposalChange({ action_kind: "create_campaign", entity_name: REAL_ACCOUNT_REF, diff: REAL_CREATION_DIFF }),
    ).toBe("Crear la campaña «Acme | Primera consulta gratis | Centro Norte»");
  });

  it("crear campaña sin plan legible todavía: frase honesta, nunca la referencia cruda", () => {
    const result = describeProposalChange({ action_kind: "create_campaign", entity_name: REAL_ACCOUNT_REF, diff: { parametro: "estado", valor_actual: "", valor_propuesto: "", diff_hash: "h" } });
    expect(result).toBe("Crear una campaña nueva");
    expect(result).not.toContain(REAL_ACCOUNT_REF);
  });

  it("presupuesto diario al alza: verbo Subir con € y sufijo al día", () => {
    expect(
      describeProposalChange({ action_kind: "update", entity_name: "x", diff: { parametro: "presupuesto_diario", valor_actual: 30, valor_propuesto: 45, diff_hash: "h" } }),
    ).toBe(`Subir el presupuesto de ${formatMoney({ amount: 30, currency: "EUR" })} a ${formatMoney({ amount: 45, currency: "EUR" })} al día`);
  });

  it("presupuesto diario a la baja: verbo Bajar", () => {
    expect(
      describeProposalChange({ action_kind: "update", entity_name: "x", diff: { parametro: "presupuesto_diario", valor_actual: 172, valor_propuesto: 120, diff_hash: "h" } }),
    ).toBe(`Bajar el presupuesto de ${formatMoney({ amount: 172, currency: "EUR" })} a ${formatMoney({ amount: 120, currency: "EUR" })} al día`);
  });

  it("pausar: estado ACTIVE→PAUSED", () => {
    expect(
      describeProposalChange({ action_kind: "update", entity_name: "x", diff: { parametro: "estado", valor_actual: "ACTIVE", valor_propuesto: "PAUSED", diff_hash: "h" } }),
    ).toBe("Pausar la campaña");
  });

  it("reanudar: estado PAUSED→ACTIVE", () => {
    expect(
      describeProposalChange({ action_kind: "update", entity_name: "x", diff: { parametro: "estado", valor_actual: "PAUSED", valor_propuesto: "ACTIVE", diff_hash: "h" } }),
    ).toBe("Reanudar la campaña");
  });
});

describe("creationDiffPayload", () => {
  it("lee el plan y el destino guardado desde diff.valor_propuesto", () => {
    expect(creationDiffPayload(REAL_CREATION_DIFF)).toEqual({ plan: REAL_CREATION_PLAN, landingUrl: "https://example.com/", rawDailyBudget: { amount: 10, currency: "EUR" } });
  });

  it("nunca lanza con un valor_propuesto que no es JSON, ni con uno numérico", () => {
    expect(creationDiffPayload({ valor_propuesto: "no es json" })).toEqual({ plan: null, landingUrl: null, rawDailyBudget: null });
    expect(creationDiffPayload({ valor_propuesto: 30 })).toEqual({ plan: null, landingUrl: null, rawDailyBudget: null });
  });

  it("un plan que no valida el contrato de creación cuenta como ausente, pero su presupuesto crudo se conserva si lo trae", () => {
    expect(creationDiffPayload({ valor_propuesto: JSON.stringify({ creation_plan: { name: "x" } }) })).toEqual({ plan: null, landingUrl: null, rawDailyBudget: null });
    expect(creationDiffPayload({ valor_propuesto: JSON.stringify({ creation_plan: { name: "x", daily_budget: { amount: "7.50", currency: "EUR" } } }) })).toEqual({ plan: null, landingUrl: null, rawDailyBudget: { amount: 7.5, currency: "EUR" } });
  });
});

describe("proposalDisplayName", () => {
  it("una actualización enseña siempre entity_name", () => {
    expect(proposalDisplayName({ action_kind: "update", entity_name: "Búsqueda Marca", diff: { parametro: "estado", valor_actual: "", valor_propuesto: "", diff_hash: "h" } })).toBe("Búsqueda Marca");
  });

  it("una creación enseña el nombre del plan, nunca la referencia cruda de la cuenta", () => {
    expect(proposalDisplayName({ action_kind: "create_campaign", entity_name: REAL_ACCOUNT_REF, diff: REAL_CREATION_DIFF })).toBe("Acme | Primera consulta gratis | Centro Norte");
  });

  it("una creación sin plan legible todavía cae a un nombre genérico, nunca a la referencia cruda", () => {
    const result = proposalDisplayName({ action_kind: "create_campaign", entity_name: REAL_ACCOUNT_REF, diff: { parametro: "estado", valor_actual: "", valor_propuesto: "", diff_hash: "h" } });
    expect(result).toBe("Campaña nueva");
    expect(result).not.toContain(REAL_ACCOUNT_REF);
  });
});

describe("proposalDailyMoney", () => {
  const impact = { amount: 999, currency: "EUR" };

  it("cambio de presupuesto: la diferencia literal propuesto − actual, nunca el impacto estimado", () => {
    expect(
      proposalDailyMoney({
        action_kind: "update",
        diff: { parametro: "presupuesto_diario", valor_actual: 172, valor_propuesto: 120, diff_hash: "h" },
        estimated_impact: impact,
        current_daily_budget: null,
      }),
    ).toEqual({ amount: -52, currency: "EUR" });
  });

  it("pausar: menos el presupuesto diario vigente, no el impacto estimado", () => {
    expect(
      proposalDailyMoney({
        action_kind: "update",
        diff: { parametro: "estado", valor_actual: "ACTIVE", valor_propuesto: "PAUSED", diff_hash: "h" },
        estimated_impact: impact,
        current_daily_budget: { amount: 143, currency: "EUR" },
      }),
    ).toEqual({ amount: -143, currency: "EUR" });
  });

  it("pausar sin presupuesto diario servido cae al impacto estimado, nunca queda sin número", () => {
    expect(
      proposalDailyMoney({
        action_kind: "update",
        diff: { parametro: "estado", valor_actual: "ACTIVE", valor_propuesto: "PAUSED", diff_hash: "h" },
        estimated_impact: { amount: -310, currency: "EUR" },
        current_daily_budget: null,
      }),
    ).toEqual({ amount: -310, currency: "EUR" });
  });

  it("crear/publicar: el presupuesto diario propuesto", () => {
    expect(
      proposalDailyMoney({
        action_kind: "create_campaign",
        diff: { parametro: "presupuesto_diario", valor_actual: 0, valor_propuesto: 30, diff_hash: "h" },
        estimated_impact: impact,
        current_daily_budget: null,
      }),
    ).toEqual({ amount: 30, currency: "EUR" });
  });

  it("crear campaña con el payload real: lee el presupuesto diario del plan, nunca «Sin coste extra» (defecto companion 0.2.20)", () => {
    expect(
      proposalDailyMoney({
        action_kind: "create_campaign",
        diff: REAL_CREATION_DIFF,
        estimated_impact: impact,
        current_daily_budget: null,
      }),
    ).toEqual({ amount: 10, currency: "EUR" });
  });

  it("crear/publicar sin presupuesto propuesto: sin coste extra (0)", () => {
    expect(
      proposalDailyMoney({
        action_kind: "create_campaign",
        diff: { parametro: "estado", valor_actual: "", valor_propuesto: "PAUSED", diff_hash: "h" },
        estimated_impact: impact,
        current_daily_budget: null,
      }),
    ).toEqual({ amount: 0, currency: "EUR" });
  });

  it("un cambio sin cifra diaria clara (p. ej. puja) no inventa dinero", () => {
    expect(
      proposalDailyMoney({
        action_kind: "update",
        diff: { parametro: "puja_maxima", valor_actual: 1.4, valor_propuesto: 1.55, diff_hash: "h" },
        estimated_impact: impact,
        current_daily_budget: null,
      }),
    ).toBeNull();
  });
});

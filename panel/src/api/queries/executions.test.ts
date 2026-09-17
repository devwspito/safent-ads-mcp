import { describe, expect, it } from "vitest";
import { unconfirmedExecutionsInterval } from "./executions";
import type { ExecutionsResponse } from "@/api/schemas/executions";

function executionsResponse(count: number): ExecutionsResponse {
  return {
    items: Array.from({ length: count }, (_, i) => ({
      execution_id: `exec_${i}`,
      proposal_id: `prop_${i}`,
      entity_name: "Campaña",
      outcome: "UNKNOWN",
      error_code: null,
      applied_value: null,
      previous_value: 0,
      estimated_impact: { amount: 0, currency: "EUR" },
      undo_deadline: null,
      started_at: new Date().toISOString(),
      finished_at: null,
      undone_at: null,
      compensating_proposal_id: null,
    })),
  };
}

/** item 10 (medido en producción, 16-sep): 10 s solo con algo en vuelo, si no 60 s. */
describe("unconfirmedExecutionsInterval", () => {
  it("devuelve 60_000 sin nada sin confirmar todavía", () => {
    expect(unconfirmedExecutionsInterval(undefined)).toBe(60_000);
    expect(unconfirmedExecutionsInterval(executionsResponse(0))).toBe(60_000);
  });

  it("devuelve 10_000 con al menos una ejecución en vuelo", () => {
    expect(unconfirmedExecutionsInterval(executionsResponse(1))).toBe(10_000);
  });
});

/** `contracts/rest-api.md` §Economía unitaria — estado en memoria para `mocks/handlers/economics.ts`
 * (mismo patrón que `fixtures/calendarEvents.ts`). */
import { DEFAULT_MOCK_BUSINESS } from "./businesses";
import type { OfferingEconomicsInput } from "@/api/schemas/economics";

interface OfferingRecord {
  offering_id: string;
  code: string;
  title: string;
  is_active: boolean;
  list_price: { amount: number; currency: string } | null;
  economics: (OfferingEconomicsInput & { updated_at: string }) | null;
}

interface RejectedRow {
  line: number;
  reason: string;
}

const offeringsByBusiness = new Map<string, OfferingRecord[]>();

function seedDefaults(): void {
  offeringsByBusiness.set(DEFAULT_MOCK_BUSINESS.business_id, [
    {
      offering_id: "off_plan_anual",
      code: "plan-anual",
      title: "Plan Anual Pro",
      is_active: true,
      list_price: { amount: 1200, currency: "EUR" },
      economics: null,
    },
    {
      offering_id: "off_plan_mensual",
      code: "plan-mensual",
      title: "Plan Mensual",
      is_active: true,
      list_price: { amount: 120, currency: "EUR" },
      economics: {
        vat_rate_pct: 21,
        delivery_cost_minor: 3_000,
        sales_cost_minor: 5_000,
        refund_rate_pct: 5,
        payment_plan: "none",
        currency: "EUR",
        updated_at: "2026-09-01T10:00:00Z",
      },
    },
  ]);
}
seedDefaults();

export function resetEconomicsFixtures(): void {
  offeringsByBusiness.clear();
  seedDefaults();
}

export function listOfferings(businessId: string): { items: OfferingRecord[] } {
  return { items: offeringsByBusiness.get(businessId) ?? [] };
}

export function updateOfferingEconomics(
  businessId: string,
  offeringId: string,
  input: OfferingEconomicsInput,
): OfferingRecord["economics"] | null {
  const offering = (offeringsByBusiness.get(businessId) ?? []).find((o) => o.offering_id === offeringId);
  if (!offering) return null;
  offering.economics = { ...input, updated_at: new Date().toISOString() };
  return offering.economics;
}

const REQUIRED_CSV_COLUMNS = ["occurred_at", "kind"];

export class CsvHeaderFixtureError extends Error {}

/** Simulación ligera de `ImportConversionsFromCsv` (crm/application/import_conversions.py) para
 * los fixtures de MSW: no reimplementa el parseo real, solo lo suficiente para que la UI tenga
 * un resumen creíble que pintar. */
export function importConversionsCsv(csvText: string): { imported: number; duplicates: number; rejected: RejectedRow[] } {
  const lines = csvText.trim().split(/\r?\n/);
  const header = (lines[0] ?? "").split(",").map((cell) => cell.trim());
  if (REQUIRED_CSV_COLUMNS.some((column) => !header.includes(column))) {
    throw new CsvHeaderFixtureError("faltan columnas obligatorias: occurred_at, kind");
  }
  const occurredAtIndex = header.indexOf("occurred_at");
  const identityIndexes = ["email", "phone", "external_ref"]
    .map((column) => header.indexOf(column))
    .filter((index) => index !== -1);
  const rejected: RejectedRow[] = [];
  let imported = 0;
  lines.slice(1).forEach((rawLine, index) => {
    const line = rawLine.trim();
    if (!line) return;
    const cells = line.split(",");
    if (!cells[occurredAtIndex]?.trim()) {
      rejected.push({ line: index + 2, reason: "occurred_at vacío" });
      return;
    }
    if (identityIndexes.length > 0 && !identityIndexes.some((identityIndex) => cells[identityIndex]?.trim())) {
      rejected.push({ line: index + 2, reason: "falta email, phone o external_ref: nada que hashear" });
      return;
    }
    imported += 1;
  });
  return { imported, duplicates: 0, rejected };
}

export function generateWebhookToken(): { token: string } {
  return { token: `whk_${Math.random().toString(36).slice(2)}` };
}

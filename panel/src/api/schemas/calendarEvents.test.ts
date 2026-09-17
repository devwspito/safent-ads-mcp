import { describe, expect, it } from "vitest";
import { calendarEventInputSchema, calendarEventSchema, calendarEventsResponseSchema } from "./calendarEvents";

const VALID_EVENT = {
  calendar_event_id: "evt_1",
  business_id: "biz_ejemplo",
  offering_id: null,
  name: "Primaria — Valencia",
  kind: "season",
  region: "Comunidad Valenciana",
  window_start: "2026-01-01",
  window_end: "2026-02-01",
  event_date: null,
  is_window_open: true,
};

describe("calendarEventSchema", () => {
  it("acepta la forma exacta de vocabulary.md §4", () => {
    expect(calendarEventSchema.safeParse(VALID_EVENT).success).toBe(true);
  });

  it("rechaza un kind fuera de season|deadline|launch|promotion", () => {
    const result = calendarEventSchema.safeParse({ ...VALID_EVENT, kind: "invalido" });
    expect(result.success).toBe(false);
  });

  it("exige is_window_open como booleano derivado, no opcional", () => {
    const withoutDerivedField: Partial<typeof VALID_EVENT> = { ...VALID_EVENT };
    delete withoutDerivedField.is_window_open;
    const result = calendarEventSchema.safeParse(withoutDerivedField);
    expect(result.success).toBe(false);
  });
});

describe("calendarEventsResponseSchema", () => {
  it("envuelve la lista en items", () => {
    expect(calendarEventsResponseSchema.safeParse({ items: [VALID_EVENT] }).success).toBe(true);
  });
});

describe("calendarEventInputSchema", () => {
  it("acepta el input mínimo: name, kind, window_start, window_end", () => {
    const result = calendarEventInputSchema.safeParse({
      name: "Lanzamiento otoño",
      kind: "launch",
      window_start: "2026-09-01",
      window_end: "2026-09-30",
    });
    expect(result.success).toBe(true);
  });

  it("region, event_date y offering_id son opcionales", () => {
    const result = calendarEventInputSchema.safeParse({
      name: "Promoción de invierno",
      kind: "promotion",
      region: "Madrid",
      window_start: "2026-12-01",
      window_end: "2026-12-31",
      event_date: "2026-12-15",
      offering_id: "off_1",
    });
    expect(result.success).toBe(true);
  });
});

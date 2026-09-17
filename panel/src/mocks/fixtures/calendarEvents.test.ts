import { describe, expect, it } from "vitest";
import type { CalendarEventInput } from "@/api/schemas/calendarEvents";
import {
  createCalendarEvent,
  deleteCalendarEvent,
  listCalendarEvents,
  updateCalendarEvent,
  validateCalendarEventInput,
} from "./calendarEvents";

describe("validateCalendarEventInput", () => {
  const validInput = { name: "Lanzamiento", kind: "launch" as const, window_start: "2026-01-01", window_end: "2026-02-01" };

  it("acepta un input válido", () => {
    expect(validateCalendarEventInput(validInput)).toBeNull();
  });

  it("rechaza un nombre fuera de 1-120 caracteres", () => {
    expect(validateCalendarEventInput({ ...validInput, name: "a".repeat(121) })).toMatch(/nombre/);
  });

  it("rechaza un kind fuera de season|deadline|launch|promotion", () => {
    const invalidKindInput = { ...validInput, kind: "temporada" } as unknown as CalendarEventInput;
    expect(validateCalendarEventInput(invalidKindInput)).toMatch(/tipo de evento/);
  });

  it("rechaza una región fuera de 2-64 caracteres", () => {
    expect(validateCalendarEventInput({ ...validInput, region: "M" })).toMatch(/región/);
  });

  it("rechaza window_start >= window_end", () => {
    expect(validateCalendarEventInput({ ...validInput, window_start: "2026-02-01", window_end: "2026-01-01" })).toMatch(/ventana/);
  });

  it("rechaza event_date anterior a window_start", () => {
    expect(validateCalendarEventInput({ ...validInput, event_date: "2025-12-31" })).toMatch(/fecha del evento/);
  });
});

describe("createCalendarEvent / listCalendarEvents / updateCalendarEvent / deleteCalendarEvent", () => {
  it("crea, lista, actualiza y borra un evento, derivando is_window_open del business propietario", () => {
    const created = createCalendarEvent("biz_test_crud", {
      name: "Promoción de prueba",
      kind: "promotion",
      window_start: "2020-01-01",
      window_end: "2020-01-31",
    });
    expect(created.is_window_open).toBe(false);

    const { items } = listCalendarEvents("biz_test_crud", false);
    expect(items.some((item) => item.calendar_event_id === created.calendar_event_id)).toBe(true);

    const updated = updateCalendarEvent("biz_test_crud", created.calendar_event_id, {
      name: "Promoción actualizada",
      kind: "promotion",
      window_start: "2099-01-01",
      window_end: "2099-01-31",
    });
    expect(updated?.name).toBe("Promoción actualizada");
    expect(updated?.is_window_open).toBe(true);

    const sameInputForOtherBusiness = { name: "Promoción actualizada", kind: "promotion" as const, window_start: "2099-01-01", window_end: "2099-01-31" };
    expect(updateCalendarEvent("otro_business", created.calendar_event_id, sameInputForOtherBusiness)).toBeNull();
    expect(deleteCalendarEvent("otro_business", created.calendar_event_id)).toBe(false);
    expect(deleteCalendarEvent("biz_test_crud", created.calendar_event_id)).toBe(true);
  });

  it("open_only y kind filtran la lista", () => {
    const { items: seasonOnly } = listCalendarEvents("biz_ejemplo", false, "season");
    expect(seasonOnly.every((item) => item.kind === "season")).toBe(true);

    const { items: openOnly } = listCalendarEvents("biz_ejemplo", true);
    expect(openOnly.every((item) => item.is_window_open)).toBe(true);
  });
});

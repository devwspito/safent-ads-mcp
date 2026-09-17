import { describe, expect, it } from "vitest";
import { calendarEventNameError, calendarEventWindowError } from "./calendarEvents";

describe("calendarEventNameError", () => {
  it("acepta un nombre dentro de 120 caracteres", () => {
    expect(calendarEventNameError("Lanzamiento otoño")).toBeNull();
  });

  it("rechaza un nombre de más de 120 caracteres", () => {
    const tooLong = "a".repeat(121);
    expect(calendarEventNameError(tooLong)).toMatch(/120 caracteres/);
  });

  it("acepta exactamente 120 caracteres", () => {
    expect(calendarEventNameError("a".repeat(120))).toBeNull();
  });
});

describe("calendarEventWindowError", () => {
  it("acepta una ventana con inicio antes que fin", () => {
    expect(calendarEventWindowError("2026-01-01", "2026-02-01")).toBeNull();
  });

  it("rechaza una ventana con fin antes o igual que el inicio", () => {
    expect(calendarEventWindowError("2026-02-01", "2026-01-01")).toMatch(/abrir antes de cerrar/);
    expect(calendarEventWindowError("2026-02-01", "2026-02-01")).toMatch(/abrir antes de cerrar/);
  });

  it("no valida hasta que ambas fechas estén rellenas", () => {
    expect(calendarEventWindowError("", "2026-02-01")).toBeNull();
    expect(calendarEventWindowError("2026-01-01", "")).toBeNull();
  });
});

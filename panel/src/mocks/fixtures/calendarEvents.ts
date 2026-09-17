/** Calendario manual en v1 (Assumption 6) — alimenta la lente `calendar_event` de Propuestas. */
import type { CalendarEventInput, CalendarEventKind } from "@/api/schemas/calendarEvents";

interface CalendarEventRecord {
  calendar_event_id: string;
  business_id: string;
  offering_id: string | null;
  name: string;
  kind: CalendarEventKind;
  region: string | null;
  window_start: string;
  window_end: string;
  event_date: string | null;
}

function futureDate(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
}

let nextId = 1;
const store: CalendarEventRecord[] = [
  {
    calendar_event_id: "evt_secundaria_madrid",
    business_id: "biz_ejemplo",
    offering_id: null,
    name: "Secundaria — Madrid",
    kind: "season",
    region: "Madrid",
    window_start: futureDate(-10),
    window_end: futureDate(6),
    event_date: futureDate(60),
  },
  {
    calendar_event_id: "evt_primaria_valencia",
    business_id: "biz_ejemplo",
    offering_id: null,
    name: "Primaria — Valencia",
    kind: "season",
    region: "Comunidad Valenciana",
    window_start: futureDate(-3),
    window_end: futureDate(14),
    event_date: null,
  },
  {
    calendar_event_id: "evt_eoi_andalucia",
    business_id: "biz_ejemplo",
    offering_id: null,
    name: "EOI Inglés — Andalucía",
    kind: "deadline",
    region: "Andalucía",
    window_start: futureDate(0),
    window_end: futureDate(30),
    event_date: null,
  },
];

function isWindowOpen(record: CalendarEventRecord): boolean {
  return new Date(record.window_end).getTime() >= Date.now();
}

function toCalendarEventResponse(record: CalendarEventRecord) {
  return { ...record, is_window_open: isWindowOpen(record) };
}

export function listCalendarEvents(businessId: string, openOnly: boolean, kind?: string) {
  const items = store
    .filter((record) => record.business_id === businessId)
    .filter((record) => !openOnly || isWindowOpen(record))
    .filter((record) => !kind || record.kind === kind)
    .map(toCalendarEventResponse);
  return { items };
}

const VALID_KINDS = new Set<string>(["season", "deadline", "launch", "promotion"]);

/** Espeja las reglas 422 de `vocabulary.md` §4: devuelve el mensaje del primer fallo, o null si es válido. */
export function validateCalendarEventInput(input: CalendarEventInput): string | null {
  if (input.name.length < 1 || input.name.length > 120) return "El nombre debe tener entre 1 y 120 caracteres.";
  if (!VALID_KINDS.has(input.kind)) return "El tipo de evento no es válido.";
  if (input.region !== undefined && (input.region.length < 2 || input.region.length > 64)) {
    return "La región debe tener entre 2 y 64 caracteres.";
  }
  if (input.window_start >= input.window_end) return "La ventana debe abrir antes de cerrar.";
  if (input.event_date && input.event_date < input.window_start) {
    return "La fecha del evento no puede ser anterior al inicio de la ventana.";
  }
  return null;
}

export function createCalendarEvent(businessId: string, input: CalendarEventInput) {
  const record: CalendarEventRecord = {
    calendar_event_id: `evt_${nextId++}`,
    business_id: businessId,
    offering_id: input.offering_id ?? null,
    name: input.name,
    kind: input.kind,
    region: input.region ?? null,
    window_start: input.window_start,
    window_end: input.window_end,
    event_date: input.event_date ?? null,
  };
  store.push(record);
  return toCalendarEventResponse(record);
}

export function updateCalendarEvent(businessId: string, calendarEventId: string, input: CalendarEventInput) {
  const record = store.find((r) => r.calendar_event_id === calendarEventId && r.business_id === businessId);
  if (!record) return null;
  record.offering_id = input.offering_id ?? null;
  record.name = input.name;
  record.kind = input.kind;
  record.region = input.region ?? null;
  record.window_start = input.window_start;
  record.window_end = input.window_end;
  record.event_date = input.event_date ?? null;
  return toCalendarEventResponse(record);
}

export function deleteCalendarEvent(businessId: string, calendarEventId: string): boolean {
  const index = store.findIndex((r) => r.calendar_event_id === calendarEventId && r.business_id === businessId);
  if (index === -1) return false;
  store.splice(index, 1);
  return true;
}

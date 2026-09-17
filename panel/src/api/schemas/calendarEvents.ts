/** `contracts/rest-api.md` / `vocabulary.md` §4 — `/calendar-events`. */
import { z } from "zod";

export const calendarEventKindSchema = z.enum(["season", "deadline", "launch", "promotion"]);
export type CalendarEventKind = z.infer<typeof calendarEventKindSchema>;

/** Calendario manual en v1 (Assumption 6): alimenta la lente `calendar_event`. */
export const calendarEventSchema = z.object({
  calendar_event_id: z.string(),
  business_id: z.string(),
  offering_id: z.string().nullable(),
  name: z.string(),
  kind: calendarEventKindSchema,
  region: z.string().nullable(),
  window_start: z.string(),
  window_end: z.string(),
  event_date: z.string().nullable(),
  is_window_open: z.boolean(),
});
export type CalendarEvent = z.infer<typeof calendarEventSchema>;

export const calendarEventsResponseSchema = z.object({
  items: z.array(calendarEventSchema),
});

export const calendarEventInputSchema = z.object({
  name: z.string(),
  kind: calendarEventKindSchema,
  region: z.string().optional(),
  window_start: z.string(),
  window_end: z.string(),
  event_date: z.string().optional(),
  offering_id: z.string().optional(),
});
export type CalendarEventInput = z.infer<typeof calendarEventInputSchema>;

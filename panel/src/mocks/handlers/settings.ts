import { http, HttpResponse } from "msw";
import {
  createCalendarEvent,
  deleteCalendarEvent,
  listCalendarEvents,
  updateCalendarEvent,
  validateCalendarEventInput,
} from "../fixtures/calendarEvents";
import { getSettings, updateSettings } from "../fixtures/settings";
import { DEFAULT_MOCK_BUSINESS } from "../fixtures/businesses";
import { API_BASE } from "./apiBase";
import type { CalendarEventInput } from "@/api/schemas/calendarEvents";

const CALENDAR_EVENT_NOT_FOUND = { error: { code: "NOT_FOUND", message: "El evento de calendario no existe." } };

function businessIdOf(request: Request): string {
  return new URL(request.url).searchParams.get("business_id") ?? DEFAULT_MOCK_BUSINESS.business_id;
}

export const settingsHandlers = [
  http.get(`${API_BASE}/settings`, () => HttpResponse.json(getSettings())),

  http.put(`${API_BASE}/settings`, async ({ request }) => {
    const body = (await request.json()) as Parameters<typeof updateSettings>[0];
    return HttpResponse.json(updateSettings(body));
  }),

  http.get(`${API_BASE}/calendar-events`, ({ request }) => {
    const url = new URL(request.url);
    const openOnly = url.searchParams.get("open_only") === "true";
    const kind = url.searchParams.get("kind") ?? undefined;
    return HttpResponse.json(listCalendarEvents(businessIdOf(request), openOnly, kind));
  }),

  http.post(`${API_BASE}/calendar-events`, async ({ request }) => {
    const body = (await request.json()) as CalendarEventInput;
    const validationError = validateCalendarEventInput(body);
    if (validationError) return HttpResponse.json({ error: { code: "VALIDATION_ERROR", message: validationError } }, { status: 422 });
    const created = createCalendarEvent(businessIdOf(request), body);
    return HttpResponse.json(created, { status: 201 });
  }),

  http.put(`${API_BASE}/calendar-events/:id`, async ({ params, request }) => {
    const body = (await request.json()) as CalendarEventInput;
    const validationError = validateCalendarEventInput(body);
    if (validationError) return HttpResponse.json({ error: { code: "VALIDATION_ERROR", message: validationError } }, { status: 422 });
    const updated = updateCalendarEvent(businessIdOf(request), String(params.id), body);
    if (!updated) return HttpResponse.json(CALENDAR_EVENT_NOT_FOUND, { status: 404 });
    return HttpResponse.json(updated);
  }),

  http.delete(`${API_BASE}/calendar-events/:id`, ({ params, request }) => {
    const ok = deleteCalendarEvent(businessIdOf(request), String(params.id));
    if (!ok) return HttpResponse.json(CALENDAR_EVENT_NOT_FOUND, { status: 404 });
    return new HttpResponse(null, { status: 204 });
  }),
];

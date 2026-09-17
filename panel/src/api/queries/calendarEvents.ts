import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient, voidResponseSchema } from "@/api/client";
import {
  calendarEventSchema,
  calendarEventsResponseSchema,
  type CalendarEventInput,
  type CalendarEventKind,
} from "@/api/schemas/calendarEvents";

export interface CalendarEventsFilters {
  openOnly?: boolean;
  kind?: CalendarEventKind;
}

export function useCalendarEvents(businessId: string, filters: CalendarEventsFilters = {}) {
  return useQuery({
    queryKey: ["calendar-events", businessId, filters.openOnly ?? false, filters.kind],
    queryFn: () =>
      apiClient.get("/calendar-events", calendarEventsResponseSchema, {
        business_id: businessId,
        open_only: filters.openOnly || undefined,
        kind: filters.kind,
      }),
    enabled: Boolean(businessId),
  });
}

function invalidateCalendarEvents(queryClient: ReturnType<typeof useQueryClient>, businessId: string) {
  void queryClient.invalidateQueries({ queryKey: ["calendar-events", businessId] });
}

export function useCreateCalendarEvent(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CalendarEventInput) =>
      apiClient.post("/calendar-events", calendarEventSchema, input, { business_id: businessId }),
    onSuccess: () => invalidateCalendarEvents(queryClient, businessId),
  });
}

export function useUpdateCalendarEvent(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { calendarEventId: string; update: CalendarEventInput }) =>
      apiClient.put(`/calendar-events/${input.calendarEventId}`, calendarEventSchema, input.update, { business_id: businessId }),
    onSuccess: () => invalidateCalendarEvents(queryClient, businessId),
  });
}

export function useDeleteCalendarEvent(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (calendarEventId: string) =>
      apiClient.delete(`/calendar-events/${calendarEventId}`, voidResponseSchema, undefined, { business_id: businessId }),
    onSuccess: () => invalidateCalendarEvents(queryClient, businessId),
  });
}

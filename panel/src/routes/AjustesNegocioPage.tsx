import { useState } from "react";
import { Link } from "react-router-dom";
import { useMe } from "@/api/queries/auth";
import { useCalendarEvents, useDeleteCalendarEvent } from "@/api/queries/calendarEvents";
import type { CalendarEvent } from "@/api/schemas/calendarEvents";
import { MarcaSection } from "@/components/brand/MarcaSection";
import { CalendarEventForm } from "@/components/settings/CalendarEventForm";
import { ConversionsCard } from "@/components/settings/ConversionsCard";
import { EconomiaSection } from "@/components/settings/EconomiaSection";
import { PageHeader } from "@/components/layout/PageHeader";
import { TypedConfirmDialog } from "@/components/common/TypedConfirmDialog";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { describeApiError } from "@/utils/apiError";
import { formatDateOnly } from "@/utils/time";
import styles from "./AjustesNegocioPage.module.css";

/**
 * «Tu negocio» — design.md §6.1.3/§6.4: lo que hoy vive bajo Marca, Ofertas y economía,
 * Calendario de eventos y Conversiones, fuera de las cuatro tareas diarias del dueño; Ajustes
 * solo enlaza aquí, discretamente. Mismo comportamiento de siempre, solo con ruta propia.
 */
export function AjustesNegocioPage() {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  return <BusinessAjustesNegocioPage key={businessId} businessId={businessId} />;
}

function BusinessAjustesNegocioPage({ businessId }: { businessId: string }) {
  const calendarEventsQuery = useCalendarEvents(businessId);
  const deleteCalendarEvent = useDeleteCalendarEvent(businessId);
  const [eventToDelete, setEventToDelete] = useState<CalendarEvent | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function handleDeleteCalendarEvent(target: CalendarEvent) {
    setEventToDelete(null);
    setDeleteError(null);
    try {
      await deleteCalendarEvent.mutateAsync(target.calendar_event_id);
    } catch (error) {
      setDeleteError(describeApiError(error));
    }
  }

  return (
    <div>
      <Link className={styles.backLink} to="/ajustes">
        ‹ Ajustes
      </Link>
      <PageHeader title="Tu negocio" />

      {businessId ? <MarcaSection businessId={businessId} /> : null}
      {businessId ? <EconomiaSection businessId={businessId} /> : null}

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Calendario de eventos</h2>
        {deleteError ? (
          <span className={styles.deleteError} role="alert">
            {deleteError}
          </span>
        ) : null}
        {(calendarEventsQuery.data?.items ?? []).map((calendarEvent) => (
          <div key={calendarEvent.calendar_event_id} className={styles.calendarEventRow}>
            <span>
              {calendarEvent.name}
              {calendarEvent.region ? ` · ${calendarEvent.region}` : ""}
            </span>
            <span>Cierra {formatDateOnly(calendarEvent.window_end)}</span>
            <button
              type="button"
              className={styles.deleteEvent}
              onClick={() => {
                setDeleteError(null);
                setEventToDelete(calendarEvent);
              }}
              aria-label={`Eliminar evento: ${calendarEvent.name}`}
            >
              Eliminar
            </button>
          </div>
        ))}
        <CalendarEventForm businessId={businessId} />
      </section>

      {businessId ? (
        <div className={styles.section}>
          <ConversionsCard businessId={businessId} />
        </div>
      ) : null}

      {eventToDelete ? (
        <TypedConfirmDialog
          title={`Eliminar "${eventToDelete.name}"`}
          description="Este evento de calendario desaparecerá y dejará de proponer campañas por él. Escribe ELIMINAR para confirmar."
          confirmLabel="Eliminar"
          confirmWord="ELIMINAR"
          danger
          onConfirm={() => void handleDeleteCalendarEvent(eventToDelete)}
          onClose={() => setEventToDelete(null)}
        />
      ) : null}
    </div>
  );
}

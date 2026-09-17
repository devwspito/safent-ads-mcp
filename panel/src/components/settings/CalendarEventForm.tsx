import { useId, useState, type FormEvent } from "react";
import { ApiRequestError } from "@/api/client";
import { useCreateCalendarEvent } from "@/api/queries/calendarEvents";
import type { CalendarEventInput, CalendarEventKind } from "@/api/schemas/calendarEvents";
import { calendarEventNameError, calendarEventWindowError } from "@/utils/calendarEvents";
import styles from "./CalendarEventForm.module.css";

interface CalendarEventFormProps {
  businessId: string;
}

const KIND_LABELS: Record<CalendarEventKind, string> = {
  season: "Temporada",
  deadline: "Fecha límite",
  launch: "Lanzamiento",
  promotion: "Promoción",
};

const KIND_OPTIONS = Object.keys(KIND_LABELS) as CalendarEventKind[];

const DEFAULT_ERROR_MESSAGE = "No se pudo crear el evento de calendario.";

function emptyForm(): CalendarEventInput {
  return { name: "", kind: "season", region: "", window_start: "", window_end: "", event_date: "" };
}

/** Alta de eventos de calendario (vocabulary.md §4/§5): la validación del cliente espeja las
 * reglas 422 del servidor, que sigue siendo quien decide de verdad. */
export function CalendarEventForm({ businessId }: CalendarEventFormProps) {
  const [form, setForm] = useState<CalendarEventInput>(emptyForm());
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [succeeded, setSucceeded] = useState(false);
  const createCalendarEvent = useCreateCalendarEvent(businessId);
  const formId = useId();

  const nameError = calendarEventNameError(form.name);
  const windowError = calendarEventWindowError(form.window_start, form.window_end);

  function update<K extends keyof CalendarEventInput>(key: K, value: CalendarEventInput[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
    setSucceeded(false);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage(null);
    setSucceeded(false);
    if (nameError || windowError) {
      setErrorMessage(nameError ?? windowError);
      return;
    }
    try {
      await createCalendarEvent.mutateAsync({
        ...form,
        name: form.name.trim(),
        region: form.region?.trim() || undefined,
        event_date: form.event_date?.trim() || undefined,
      });
      setForm(emptyForm());
      setSucceeded(true);
    } catch (error) {
      setErrorMessage(error instanceof ApiRequestError ? error.message : DEFAULT_ERROR_MESSAGE);
    }
  }

  const canSubmit = form.name.trim().length > 0 && Boolean(form.window_start) && Boolean(form.window_end) && !nameError && !windowError;

  return (
    <form className={styles.form} onSubmit={(event) => void handleSubmit(event)}>
      <div className={styles.row}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${formId}-name`}>
            Nombre
          </label>
          <input
            id={`${formId}-name`}
            className={styles.input}
            type="text"
            maxLength={120}
            value={form.name}
            onChange={(e) => update("name", e.target.value)}
            aria-invalid={Boolean(nameError)}
            aria-describedby={nameError ? `${formId}-name-error` : undefined}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${formId}-kind`}>
            Tipo
          </label>
          <select id={`${formId}-kind`} className={styles.select} value={form.kind} onChange={(e) => update("kind", e.target.value as CalendarEventKind)}>
            {KIND_OPTIONS.map((kind) => (
              <option key={kind} value={kind}>
                {KIND_LABELS[kind]}
              </option>
            ))}
          </select>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${formId}-region`}>
            Región (opcional)
          </label>
          <input
            id={`${formId}-region`}
            className={styles.input}
            type="text"
            value={form.region ?? ""}
            onChange={(e) => update("region", e.target.value)}
          />
        </div>
      </div>

      <div className={styles.row}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${formId}-window-start`}>
            Ventana — desde
          </label>
          <input
            id={`${formId}-window-start`}
            className={styles.input}
            type="date"
            value={form.window_start}
            onChange={(e) => update("window_start", e.target.value)}
            aria-invalid={Boolean(windowError)}
            aria-describedby={windowError ? `${formId}-window-error` : undefined}
          />
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${formId}-window-end`}>
            Ventana — hasta
          </label>
          <input
            id={`${formId}-window-end`}
            className={styles.input}
            type="date"
            value={form.window_end}
            onChange={(e) => update("window_end", e.target.value)}
            aria-invalid={Boolean(windowError)}
            aria-describedby={windowError ? `${formId}-window-error` : undefined}
          />
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${formId}-event-date`}>
            Fecha del evento (opcional)
          </label>
          <input
            id={`${formId}-event-date`}
            className={styles.input}
            type="date"
            value={form.event_date ?? ""}
            onChange={(e) => update("event_date", e.target.value)}
          />
        </div>
      </div>

      <button type="submit" className={styles.save} disabled={!canSubmit || createCalendarEvent.isPending}>
        {createCalendarEvent.isPending ? "Añadiendo…" : "Añadir evento"}
      </button>

      {succeeded ? (
        <span className={styles.status} role="status">
          Evento añadido.
        </span>
      ) : null}
      {nameError ? (
        <span id={`${formId}-name-error`} className={styles.error} role="alert">
          {nameError}
        </span>
      ) : null}
      {windowError ? (
        <span id={`${formId}-window-error`} className={styles.error} role="alert">
          {windowError}
        </span>
      ) : null}
      {errorMessage && !nameError && !windowError ? (
        <span className={styles.error} role="alert">
          {errorMessage}
        </span>
      ) : null}
    </form>
  );
}

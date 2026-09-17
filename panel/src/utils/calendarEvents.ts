/** Validación cliente del formulario de calendario — espeja las reglas 422 de vocabulary.md §4;
 * el servidor sigue siendo quien decide de verdad (defensa en profundidad). */
const NAME_MAX_LENGTH = 120;

export function calendarEventNameError(name: string): string | null {
  if (name.length > NAME_MAX_LENGTH) return `El nombre debe tener como máximo ${NAME_MAX_LENGTH} caracteres.`;
  return null;
}

export function calendarEventWindowError(windowStart: string, windowEnd: string): string | null {
  if (!windowStart || !windowEnd) return null;
  if (windowStart >= windowEnd) return "La ventana debe abrir antes de cerrar.";
  return null;
}

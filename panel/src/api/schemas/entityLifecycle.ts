/**
 * `POST /entities/{entity_ref}/pause|resume|delete` (spec 002-panel-simple, commit 06800d8):
 * el clic del propietario ES la autorización humana explícita, sin propuesta intermedia — mismo
 * chokepoint de escritura que una propuesta aprobada, por eso pausar/reanudar admite
 * `POST /executions/{id}/undo` dentro de la ventana de gracia y borrar nunca la tiene
 * (`UndoGracePolicy.grace_for` devuelve `None` para una transición a `DELETED`).
 */
import { z } from "zod";

export const pauseResumeResponseSchema = z.object({
  execution_id: z.string(),
  undo_deadline: z.string().nullable(),
});
export type PauseResumeResponse = z.infer<typeof pauseResumeResponseSchema>;

export const deleteEntityResponseSchema = z.object({
  execution_id: z.string(),
});
export type DeleteEntityResponse = z.infer<typeof deleteEntityResponseSchema>;

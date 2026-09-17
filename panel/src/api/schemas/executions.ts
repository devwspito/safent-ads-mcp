/** `contracts/rest-api.md` §Ejecución, deshacer y freno (US2), reconciled v2. */
import { z } from "zod";
import { moneySchema } from "@/api/schemas";

export const executionOutcomeSchema = z.enum([
  "CLAIMED",
  "RUNNING",
  "UNKNOWN",
  "SUCCEEDED",
  "FAILED",
  "SKIPPED_DRIFT",
  "BLOCKED_GUARDRAIL",
  "BLOCKED_BRAKE",
  "UNDONE",
]);
export type ExecutionOutcome = z.infer<typeof executionOutcomeSchema>;

export const executionSchema = z.object({
  execution_id: z.string(),
  proposal_id: z.string(),
  entity_name: z.string(),
  outcome: executionOutcomeSchema,
  error_code: z.string().nullable(),
  applied_value: z.union([z.number(), z.string()]).nullable(),
  previous_value: z.union([z.number(), z.string()]).nullable(),
  estimated_impact: moneySchema,
  undo_deadline: z.string().nullable(),
  started_at: z.string(),
  finished_at: z.string().nullable(),
  undone_at: z.string().nullable(),
  compensating_proposal_id: z.string().nullable(),
});
export type Execution = z.infer<typeof executionSchema>;
export const executionsResponseSchema = z.object({ items: z.array(executionSchema) });
export type ExecutionsResponse = z.infer<typeof executionsResponseSchema>;

/** Dentro de la gracia cancela; ya ejecutada, compensa con una propuesta de restauración. */
export const undoKindSchema = z.enum(["cancelled", "compensated"]);
export type UndoKind = z.infer<typeof undoKindSchema>;

export const undoResponseSchema = z.object({
  undo_kind: undoKindSchema,
  execution_id: z.string(),
  compensating_proposal_id: z.string().nullable(),
});
export type UndoResponse = z.infer<typeof undoResponseSchema>;

export const batchUndoResultSchema = z.object({
  execution_id: z.string(),
  ok: z.boolean(),
  undo_kind: undoKindSchema.optional(),
  compensating_proposal_id: z.string().nullable().optional(),
  error_code: z.string().optional(),
});

export const batchUndoResponseSchema = z.object({
  results: z.array(batchUndoResultSchema),
});
export type BatchUndoResponse = z.infer<typeof batchUndoResponseSchema>;

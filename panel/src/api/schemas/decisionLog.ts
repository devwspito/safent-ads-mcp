/** `contracts/rest-api.md` §Auditoría y salud. */
import { z } from "zod";

export const actorKindSchema = z.enum(["owner", "rule_engine", "agent", "system"]);
export type ActorKind = z.infer<typeof actorKindSchema>;

export const decisionLogEventTypeSchema = z.enum([
  "SignalEmitted",
  "ProposalRaised",
  "ProposalApproved",
  "ProposalRejected",
  "ProposalExpired",
  "ExecutionSucceeded",
  "ExecutionFailed",
  "ExecutionUndone",
  "RuleFired",
  "EmergencyBrakeEngaged",
  "EmergencyBrakeReleased",
  "PlatformAccountSuspended",
  "AdEntityDrifted",
]);
export type DecisionLogEventType = z.infer<typeof decisionLogEventTypeSchema>;

export const decisionLogEntrySchema = z.object({
  seq: z.number().int(),
  business_id: z.string(),
  event_type: decisionLogEventTypeSchema,
  entity_ref: z.string().nullable(),
  entity_name: z.string().nullable(),
  actor_kind: actorKindSchema,
  actor_label: z.string(),
  proposal_id: z.string().nullable(),
  summary: z.string(),
  before: z.record(z.unknown()).nullable(),
  after: z.record(z.unknown()).nullable(),
  occurred_at: z.string(),
});
export type DecisionLogEntry = z.infer<typeof decisionLogEntrySchema>;

export const decisionLogResponseSchema = z.object({
  items: z.array(decisionLogEntrySchema),
  next_cursor: z.string().nullable(),
});

export const decisionLogVerifySchema = z.object({
  verified_through_seq: z.number().int(),
  chain_ok: z.boolean(),
  checked_at: z.string(),
});
export type DecisionLogVerify = z.infer<typeof decisionLogVerifySchema>;
